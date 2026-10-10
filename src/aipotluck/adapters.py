"""Fixed upstream adapters for a contribution node.

A node configures exactly one adapter against one upstream. The review's
guidance is "do one supply type, one consumption protocol and one data path
first": a node that can be pointed
at an arbitrary URL from a remote message is a request-forgery primitive, so the
endpoint and credential are operator configuration, never task input.

Every adapter counts its own upstream calls. That counter is the only honest
evidence for "the retry did not make a second model call" — inferring it from
the absence of an error proves nothing.
"""

from __future__ import annotations

import codecs
import http.client
import json
import socket
import ssl
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from aipotluck.tls import client_ssl_context

from .canonical import loads_strict
from .fake_upstream import FakeResponse, FakeUpstream, UpstreamError, UpstreamQuotaExceeded
from .metering import NormalizedUsage, normalize_anthropic_usage, normalize_openai_usage

#: Default wall-clock budget for one buffered upstream call.
DEFAULT_TIMEOUT_S = 60.0

#: Largest upstream body accepted, in bytes. A provider response larger than this
#: is refused rather than buffered without limit.
MAX_UPSTREAM_BODY_BYTES = 8 * 1024 * 1024

#: Bytes requested per readable read of a streaming body. Streaming must not wait
#: for a full buffer to fill, so `read1` is used with a modest ceiling; the value
#: bounds one read, not one event.
SSE_READ_SIZE = 4096


class AdapterError(RuntimeError):
    """Raised when an upstream call fails.

    Distinct from a transport error: the model call was attempted and did not
    produce a usable result, which is what makes it safe to consider a retry.
    """


class AdapterUnavailable(AdapterError):
    """Raised when the upstream cannot be reached at all.

    Nothing was dispatched — the TCP connection itself failed — so the caller
    can treat the attempt as never having reached the upstream and retry it
    elsewhere without any risk of paying twice.
    """


class AdapterOutcomeUnknown(AdapterError):
    """Raised when a request was dispatched but its outcome could not be learned.

    The request bytes were sent — the connect succeeded — and the answer was
    lost: a read timeout, a connection reset mid-response, a stream that ended
    before its terminal event. Whether the upstream executed the call is
    unknowable from here, so the caller must NOT classify this as a clean
    failure: retrying an attempt whose upstream may still be running is how a
    single user request becomes two upstream bills (plan v0.3 §5.1 G04). The
    connector turns this into an ``UNKNOWN`` execution — usage stays unknown,
    never zero, and the center's reconciliation decides the outcome.
    """


class AdapterQuotaExceeded(AdapterError):
    """Raised when the upstream reports quota exhaustion.

    Retrying a quota error is pointless, so the connector must not treat this as
    a transient failure.
    """


@dataclass(frozen=True)
class AdapterResult:
    """One successful upstream completion."""

    chunks: tuple[str, ...]
    usage: NormalizedUsage | None
    provider_style: Literal["openai", "anthropic"]
    upstream_calls: int

    @property
    def text(self) -> str:
        """Return the concatenated completion text."""
        return "".join(self.chunks)


@dataclass(frozen=True)
class StreamChunk:
    """One increment of a streaming completion.

    A stream is a sequence of these; concatenating every `text` in order
    reproduces the completion. Exactly one element — the last — has
    `is_final=True`, which is what lets a caller treat "the iterator ended" and
    "the upstream said it was done" as different facts. `usage` is only ever set
    on the final chunk: providers report it once, at the end, and pretending
    otherwise would invite billing a partial count as if it were the total.
    """

    text: str = ""
    is_final: bool = False
    usage: NormalizedUsage | None = None


@dataclass(frozen=True)
class SSEFrame:
    """One decoded Server-Sent Events frame.

    `event` is the `event:` field when the upstream names one, else `None`.
    `data` is every `data:` line of the frame joined with newlines, which is the
    shape the SSE specification requires and what a multi-line JSON payload
    relies on.
    """

    event: str | None
    data: str


class _SSEAccumulator:
    """Accumulates SSE lines into frames.

    Kept as one small state machine rather than inline logic because the
    specification's fiddly rules — a blank line terminates a frame, `data:` lines
    concatenate, a leading colon is a comment, an event name alone dispatches
    nothing — are exactly the parts that are easy to get subtly wrong.
    """

    def __init__(self) -> None:
        self._event: str | None = None
        self._data: list[str] = []

    def feed(self, line: str) -> SSEFrame | None:
        """Consume one line, returning a completed frame when one ends.

        Args:
            line: One line of the stream, without its trailing newline.

        Returns:
            The frame if this line terminated one, else `None`.
        """
        if line.endswith("\r"):
            line = line[:-1]
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        return None

    def flush(self) -> SSEFrame | None:
        """Dispatch a frame left pending when the stream ends without a blank line."""
        return self._dispatch()

    def _dispatch(self) -> SSEFrame | None:
        """Return and reset the pending frame, if it carries data."""
        if not self._data:
            self._event = None
            return None
        frame = SSEFrame(event=self._event, data="\n".join(self._data))
        self._event = None
        self._data = []
        return frame


def iter_sse_events(chunks: Iterable[bytes]) -> Iterator[SSEFrame]:
    """Decode SSE frames from an iterable of raw byte blocks.

    The transport hands over bytes in whatever sizes the kernel produced, so a
    frame routinely arrives split across blocks and several frames can share one
    block. Framing is therefore done here on a growing text buffer rather than
    assumed to align with reads, and a UTF-8 sequence split across blocks is held
    until it completes instead of being decoded as replacement characters.

    Args:
        chunks: Successive byte blocks, in order; an empty block signals nothing
            by itself, so the caller ends the iteration at end of body.

    Yields:
        Each complete frame, in order. A trailing frame with data but no final
        blank line is still yielded, so a truncated payload is surfaced to the
        caller's validator rather than silently dropped.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    accumulator = _SSEAccumulator()
    buffer = ""
    for block in chunks:
        buffer += decoder.decode(block)
        while True:
            newline = buffer.find("\n")
            if newline < 0:
                break
            frame = accumulator.feed(buffer[:newline])
            buffer = buffer[newline + 1 :]
            if frame is not None:
                yield frame
    buffer += decoder.decode(b"", final=True)
    if buffer:
        for line in buffer.split("\n"):
            frame = accumulator.feed(line)
            if frame is not None:
                yield frame
    final = accumulator.flush()
    if final is not None:
        yield final


class Adapter(Protocol):
    """One configured upstream a node may execute against."""

    #: Stable adapter name, advertised in `node.hello`.
    name: str
    #: Model identifier this adapter is configured for.
    model: str

    @property
    def supports_streaming(self) -> bool:
        """Whether `stream` may be used instead of `complete`.

        Read by the connector to decide whether a `stream_strict` request can be
        satisfied incrementally. An adapter that can only answer in one piece
        reports `False`, and the connector falls back to the buffered path rather
        than inventing a stream it cannot honour.
        """
        ...

    def complete(self, prompt: str, *, request_digest: str) -> AdapterResult:
        """Run one buffered completion.

        Args:
            prompt: The request text.
            request_digest: Digest of the request, recorded so a test can prove a
                retry re-sent identical input.

        Returns:
            The completion and its usage.

        Raises:
            AdapterQuotaExceeded: If the upstream reports quota exhaustion.
            AdapterUnavailable: If the upstream cannot be reached (nothing was
                dispatched).
            AdapterOutcomeUnknown: If the request was dispatched but its outcome
                could not be learned; the caller must not retry it blindly.
            AdapterError: On any other upstream failure — a definitive answer
                from the upstream that it executed and failed.
        """
        ...

    def stream(self, prompt: str, *, request_digest: str) -> Iterator[StreamChunk]:
        """Run one streaming completion, yielding text as the upstream produces it.

        Args:
            prompt: The request text.
            request_digest: Digest of the request, recorded so a test can prove a
                retry re-sent identical input.

        Yields:
            `StreamChunk`s in order. The last has `is_final=True` and is the only
            one whose `usage` may be set.

        Raises:
            AdapterQuotaExceeded: If the upstream reports quota exhaustion.
            AdapterUnavailable: If the upstream cannot be reached (nothing was
                dispatched).
            AdapterOutcomeUnknown: If the request was dispatched but the stream
                was lost or ended before its terminal event.
            AdapterError: On any other upstream failure, including a provider
                error event; a truncated stream is never reported as a
                completed one.
        """
        ...


@dataclass
class FakeUpstreamAdapter:
    """Adapter over the controllable fake upstream.

    Used for the whole demo path so the network, spool, and settlement code run
    for real while the model stays free and scriptable.
    """

    upstream: FakeUpstream = field(default_factory=FakeUpstream)
    name: str = "fake-upstream"
    model: str = "fake-model-v1"

    @property
    def supports_streaming(self) -> bool:
        """The fake upstream can always emit its script incrementally."""
        return True

    def complete(self, prompt: str, *, request_digest: str) -> AdapterResult:
        """Return the next scripted response.

        Args:
            prompt: Ignored; the fake upstream answers from its script.
            request_digest: Digest recorded by the fake upstream for assertions.

        Returns:
            The scripted completion.

        Raises:
            AdapterQuotaExceeded: When the scripted fault is quota exhaustion.
            AdapterError: When the script is exhausted or the fault is an error
                before output. An exhausted script means an extra upstream call
                was made, which is a bug in the caller rather than the upstream.
        """
        del prompt
        try:
            response: FakeResponse = self.upstream.execute(request_digest)
        except UpstreamQuotaExceeded as exc:
            raise AdapterQuotaExceeded(str(exc)) from exc
        except UpstreamError as exc:
            raise AdapterError(str(exc)) from exc
        except RuntimeError as exc:
            raise AdapterError(str(exc)) from exc
        chunks = tuple(response.text_chunks)
        if not chunks and self.upstream.fault == "break_before_first_token":
            raise AdapterError("fake upstream produced no output")
        return AdapterResult(
            chunks=chunks,
            usage=response.usage,
            provider_style=response.provider_style,
            upstream_calls=self.upstream.call_count,
        )

    def stream(self, prompt: str, *, request_digest: str) -> Iterator[StreamChunk]:
        """Emit the next scripted response one block at a time.

        The script is consumed exactly once per call, through the same
        `FakeUpstream.execute` that `complete` uses, so the upstream call counter
        means the same thing on both paths: one call consumed one scripted
        response. The scripted fault mode still governs, so a mid-stream break
        surfaces as `AdapterError` rather than a short-but-successful answer.

        Args:
            prompt: Ignored; the fake upstream answers from its script.
            request_digest: Digest recorded by the fake upstream for assertions.

        Yields:
            The scripted blocks, then one final chunk. The final chunk carries
            the scripted usage unless the fault drops it or the script has none.

        Raises:
            AdapterQuotaExceeded: When the scripted fault is quota exhaustion.
            AdapterError: When the script is exhausted, breaks mid-stream, or
                produces no output at all.
        """
        del prompt
        try:
            response: FakeResponse = self.upstream.execute(request_digest)
        except UpstreamQuotaExceeded as exc:
            raise AdapterQuotaExceeded(str(exc)) from exc
        except UpstreamError as exc:
            raise AdapterError(str(exc)) from exc
        except RuntimeError as exc:
            raise AdapterError(str(exc)) from exc
        if not response.text_chunks and self.upstream.fault == "break_before_first_token":
            raise AdapterError("fake upstream produced no output")
        emitted = False
        try:
            for event in self.upstream.stream(response):
                if event.get("type") == "delta":
                    text = str(event.get("text", ""))
                    if text:
                        emitted = True
                        yield StreamChunk(text=text)
                elif event.get("type") == "usage":
                    usage = event.get("usage")
                    yield StreamChunk(
                        is_final=True,
                        usage=usage if isinstance(usage, NormalizedUsage) else None,
                    )
                    return
        except UpstreamError as exc:
            raise AdapterError(str(exc)) from exc
        if not emitted:
            raise AdapterError("fake upstream produced no output")
        yield StreamChunk(is_final=True)

    @property
    def upstream_calls(self) -> int:
        """Return how many upstream calls this adapter has made."""
        return self.upstream.call_count


@dataclass(frozen=True)
class HttpUpstreamAdapter:
    """Adapter for one fixed HTTPS endpoint with one operator-held credential.

    This is the only place an upstream credential exists. It is operator
    configuration on the node; the center never sees it. `redacted()` exists so
    logs and error messages can name the endpoint without echoing the key.
    """

    endpoint: str
    api_key: str
    model: str
    provider_style: Literal["openai", "anthropic"] = "openai"
    name: str = "http-upstream"
    #: Thinking models can spend thousands of tokens reasoning before any text
    #: appears, so the completion budget must cover thinking plus the answer.
    max_tokens: int = 1024
    timeout_s: float = DEFAULT_TIMEOUT_S
    #: Test seam: allows a local stub server with a self-signed certificate.
    ssl_context: ssl.SSLContext | None = None

    @property
    def upstream_calls(self) -> int:
        """Return the number of calls made; kept for parity with the fake adapter.

        The HTTP adapter is stateless by design — a node never needs to know how
        many calls it has made, because the node's SQLite execution table is the
        durable record. This property exists so both adapters satisfy the same
        shape in tests and contains no separate counter to drift.
        """
        return 0

    @property
    def supports_streaming(self) -> bool:
        """The HTTP adapter can read a provider's SSE stream incrementally."""
        return True

    def redacted(self) -> dict[str, Any]:
        """Return a log-safe view with the credential masked."""
        parts = urlsplit(self.endpoint)
        return {
            "adapter": self.name,
            "host": parts.hostname,
            "path": parts.path,
            "model": self.model,
            "provider_style": self.provider_style,
        }

    def complete(self, prompt: str, *, request_digest: str) -> AdapterResult:
        """Run one buffered completion against the configured endpoint.

        Args:
            prompt: The request text.
            request_digest: Digest of the request; recorded only in the request
                ID header so the upstream can correlate, never in the body.

        Returns:
            The completion and its usage.

        Raises:
            AdapterUnavailable: If the connection fails or times out.
            AdapterQuotaExceeded: On HTTP 429.
            AdapterError: On any other non-2xx response or an unparseable body.
        """
        del request_digest
        parts = self._endpoint_parts()
        body = self._encode_body(self._request_body(prompt, stream=False))
        headers = self._headers(len(body), streaming=False)
        connection = self._connect(parts)
        try:
            try:
                # The connect is explicit so the dispatch boundary is exact: a
                # failure here provably reached no upstream, while a failure
                # after it may have delivered the request.
                connection.connect()
            except (OSError, http.client.HTTPException) as exc:
                raise AdapterUnavailable(
                    f"upstream {parts.hostname} is unreachable: {exc}"
                ) from exc
            try:
                connection.request("POST", parts.path or "/", body=body, headers=headers)
                response = connection.getresponse()
                raw = response.read(MAX_UPSTREAM_BODY_BYTES + 1)
            except (OSError, http.client.HTTPException) as exc:
                # The request bytes were sent; whether the upstream executed the
                # call is unknowable, so this is outcome-unknown, not a clean
                # failure — retrying it could double-execute the call.
                raise AdapterOutcomeUnknown(
                    f"upstream response was lost after the request was sent: {exc}"
                ) from exc
        finally:
            connection.close()
        if len(raw) > MAX_UPSTREAM_BODY_BYTES:
            raise AdapterError(
                f"upstream response exceeds {MAX_UPSTREAM_BODY_BYTES} bytes; "
                "refusing to buffer it without limit"
            )
        self._raise_for_status(response.status, parts)
        return self._parse(raw)

    def stream(self, prompt: str, *, request_digest: str) -> Iterator[StreamChunk]:
        """Run one streaming completion against the configured endpoint.

        The request asks the provider for its native stream (`stream: true`,
        with OpenAI's `stream_options.include_usage` so the terminal usage frame
        is sent). Body bytes are read as they arrive and framed into SSE events;
        each text increment is yielded immediately so the caller can publish it
        before the model has finished. Only a stream that reaches its terminal
        event yields a final chunk — a connection that ends first raises, because
        a truncated answer presented as complete would be indistinguishable from
        a short one.

        Args:
            prompt: The request text.
            request_digest: Digest of the request; accepted for parity with
                `complete` and not sent in the body.

        Yields:
            `StreamChunk`s in order; the last has `is_final=True` and carries the
            provider's usage when it reported one.

        Raises:
            AdapterUnavailable: If the connection cannot be established.
            AdapterQuotaExceeded: On HTTP 429.
            AdapterError: On any other non-2xx response, a malformed frame, or a
                stream that ends before its terminal event.
        """
        del request_digest
        parts = self._endpoint_parts()
        body = self._encode_body(self._request_body(prompt, stream=True))
        headers = self._headers(len(body), streaming=True)
        connection = self._connect(parts)
        try:
            try:
                connection.connect()
            except (OSError, http.client.HTTPException) as exc:
                raise AdapterUnavailable(
                    f"upstream {parts.hostname} is unreachable: {exc}"
                ) from exc
            try:
                connection.request("POST", parts.path or "/", body=body, headers=headers)
                response = connection.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                # Same dispatch boundary as `complete`: once the request is sent,
                # a lost answer is outcome-unknown rather than unreachable.
                raise AdapterOutcomeUnknown(
                    f"upstream response was lost after the request was sent: {exc}"
                ) from exc
            self._raise_for_status(response.status, parts)
            try:
                yield from self._consume_stream(iter_sse_events(self._iter_body(response)))
            except (OSError, http.client.HTTPException) as exc:
                # The call was made and answered with headers; a failure now is
                # an unknown outcome, not a clean one.
                raise AdapterOutcomeUnknown(
                    f"upstream stream failed after the request was sent: {exc}"
                ) from exc
        finally:
            connection.close()

    def _endpoint_parts(self) -> Any:
        """Return the parsed endpoint.

        Raises:
            AdapterError: If the endpoint is not an absolute http(s) URL.
        """
        parts = urlsplit(self.endpoint)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise AdapterError(f"upstream endpoint is not an absolute URL: {self.endpoint!r}")
        return parts

    def _connect(self, parts: Any) -> http.client.HTTPConnection:
        """Open a connection to the configured endpoint."""
        host = str(parts.hostname)
        if parts.scheme == "https":
            context = self.ssl_context or client_ssl_context()
            return http.client.HTTPSConnection(
                host,
                parts.port or 443,
                timeout=self.timeout_s,
                context=context,
            )
        return http.client.HTTPConnection(str(host), parts.port or 80, timeout=self.timeout_s)

    def _encode_body(self, payload: dict[str, Any]) -> bytes:
        """Serialize a request body to its exact wire bytes."""
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    def _headers(self, content_length: int, *, streaming: bool) -> dict[str, str]:
        """Return the request headers for one call.

        `complete` and `stream` differ only in the `accept` value and the body's
        `stream` flag, so the credential and version headers are built once here
        rather than duplicated per path.

        Args:
            content_length: Length of the serialized body.
            streaming: Whether the response is expected to be SSE.

        Returns:
            The header mapping.
        """
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream" if streaming else "application/json",
            "content-length": str(content_length),
            "authorization": f"Bearer {self.api_key}",
        }
        if self.provider_style == "anthropic":
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = "2023-06-01"
        return headers

    def _raise_for_status(self, status: int, parts: Any) -> None:
        """Raise the adapter error matching an HTTP status.

        Raises:
            AdapterQuotaExceeded: On HTTP 429.
            AdapterError: On any other non-2xx status.
        """
        if status == 429:
            raise AdapterQuotaExceeded("upstream reported quota exhaustion (HTTP 429)")
        if not 200 <= status < 300:
            raise AdapterError(f"upstream returned HTTP {status} for {parts.path or '/'}")

    def _iter_body(self, response: Any) -> Iterator[bytes]:
        """Yield the response body as it arrives.

        `read1` is used rather than `read` on purpose: `read` blocks until it has
        the requested number of bytes or the body ends, which would turn a stream
        back into a buffered read. `read1` returns as soon as any bytes are
        available, so text reaches the caller while the model is still writing.

        Args:
            response: The open `http.client.HTTPResponse`.

        Yields:
            Successive byte blocks; iteration ends at end of body.
        """
        while True:
            block = response.read1(SSE_READ_SIZE)
            if not block:
                return
            yield block

    def _request_body(self, prompt: str, *, stream: bool) -> dict[str, Any]:
        """Return the provider-shaped request body.

        Args:
            prompt: The request text.
            stream: Whether to ask for the provider's native stream. The OpenAI
                style additionally requests the terminal usage frame, which is
                otherwise omitted from streams.
        """
        if self.provider_style == "anthropic":
            return {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "stream": stream,
            }
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    def _consume_stream(self, frames: Iterable[SSEFrame]) -> Iterator[StreamChunk]:
        """Turn decoded SSE frames into chunks for the configured provider style."""
        if self.provider_style == "anthropic":
            yield from self._consume_anthropic_stream(frames)
        else:
            yield from self._consume_openai_stream(frames)

    def _consume_openai_stream(self, frames: Iterable[SSEFrame]) -> Iterator[StreamChunk]:
        """Decode an OpenAI Chat Completions SSE stream.

        Raises:
            AdapterError: If a frame is malformed or the stream ends before
                `[DONE]`.
        """
        usage: NormalizedUsage | None = None
        saw_done = False
        for frame in frames:
            data = frame.data.strip()
            if data == "[DONE]":
                saw_done = True
                break
            payload = self._decode_stream_payload(data)
            raw_usage = payload.get("usage")
            if raw_usage is not None:
                # The usage frame carries empty choices and arrives before
                # [DONE]; it is the last place the totals appear.
                usage = normalize_openai_usage(_as_mapping(raw_usage))
            text = _openai_delta_text(payload)
            if text:
                yield StreamChunk(text=text)
        if not saw_done:
            # The request was dispatched, so whether the upstream completed (and
            # billed) is unknowable — an outcome-unknown, not a clean failure.
            raise AdapterOutcomeUnknown(
                "upstream stream ended before [DONE]; refusing to treat a "
                "truncated stream as a completion"
            )
        yield StreamChunk(is_final=True, usage=usage)

    def _consume_anthropic_stream(self, frames: Iterable[SSEFrame]) -> Iterator[StreamChunk]:
        """Decode an Anthropic Messages SSE stream.

        Anthropic splits usage across two events — input totals in
        `message_start`, the final output count in `message_delta` — so both are
        retained and combined at `message_stop`, when the whole answer exists.

        Raises:
            AdapterError: If a frame is malformed, the stream reports an error,
                or it ends before `message_stop`.
        """
        start_usage: dict[str, Any] | None = None
        delta_usage: dict[str, Any] | None = None
        saw_stop = False
        for frame in frames:
            payload = self._decode_stream_payload(frame.data)
            kind = payload.get("type")
            if kind == "message_start":
                message = payload.get("message")
                if isinstance(message, dict):
                    start_usage = _as_optional_mapping(message.get("usage"))
            elif kind == "content_block_delta":
                delta = payload.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    text = delta.get("text")
                    if text:
                        yield StreamChunk(text=str(text))
            elif kind == "message_delta":
                delta_usage = _as_optional_mapping(payload.get("usage"))
            elif kind == "message_stop":
                saw_stop = True
                break
            elif kind == "error":
                raise AdapterError(
                    f"upstream anthropic stream reported an error: {payload.get('error')}"
                )
        if not saw_stop:
            # The request was dispatched, so whether the upstream completed (and
            # billed) is unknowable — an outcome-unknown, not a clean failure.
            raise AdapterOutcomeUnknown(
                "upstream stream ended before message_stop; refusing to treat a "
                "truncated stream as a completion"
            )
        yield StreamChunk(is_final=True, usage=_merge_anthropic_usage(start_usage, delta_usage))

    def _decode_stream_payload(self, data: str) -> dict[str, Any]:
        """Parse one SSE frame's data as a JSON object.

        Raises:
            AdapterError: If the frame carries no data, is not strict JSON, or is
                not a JSON object.
        """
        if not data:
            raise AdapterError("upstream SSE frame carries no data")
        try:
            payload = loads_strict(data)
        except Exception as exc:
            raise AdapterError(f"upstream SSE frame is not strict JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise AdapterError("upstream SSE frame must be a JSON object")
        return payload

    def _parse(self, raw: bytes) -> AdapterResult:
        """Parse a provider response body.

        Raises:
            AdapterError: If the body is not strict JSON or lacks a completion.
        """
        try:
            payload = loads_strict(raw)
        except Exception as exc:
            raise AdapterError(f"upstream body is not strict JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise AdapterError("upstream body must be a JSON object")
        return self._from_payload(payload)

    def _from_payload(self, payload: dict[str, Any]) -> AdapterResult:
        """Extract text and usage from a provider response.

        Raises:
            AdapterError: If the response lacks a usable completion or usage.
        """
        if self.provider_style == "anthropic":
            blocks = payload.get("content")
            if not isinstance(blocks, list):
                raise AdapterError("anthropic response has no content list")
            text = "".join(
                str(block.get("text", ""))
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
            raw_usage = payload.get("usage")
            usage = (
                normalize_anthropic_usage(_as_mapping(raw_usage))
                if raw_usage is not None
                else None
            )
        else:
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                raise AdapterError("openai response has no choices")
            first = choices[0]
            if not isinstance(first, dict):
                raise AdapterError("openai choice is not an object")
            message = first.get("message")
            if not isinstance(message, dict):
                raise AdapterError("openai choice has no message")
            text = str(message.get("content", ""))
            raw_usage = payload.get("usage")
            usage = (
                normalize_openai_usage(_as_mapping(raw_usage)) if raw_usage is not None else None
            )
        if not text:
            raise AdapterError("upstream returned an empty completion")
        return AdapterResult(
            chunks=(text,),
            usage=usage,
            provider_style=self.provider_style,
            upstream_calls=1,
        )


def _as_mapping(value: object) -> dict[str, Any]:
    """Return `value` as a mapping.

    Raises:
        AdapterError: If the value is not a JSON object.
    """
    if not isinstance(value, dict):
        raise AdapterError("upstream usage block is not a JSON object")
    return dict(value)


def _as_optional_mapping(value: object) -> dict[str, Any] | None:
    """Return `value` as a mapping, or `None` when it is absent or not an object.

    Used where a missing block is legitimate — an Anthropic `message_delta` may
    carry no usage — so absence must not be an error.
    """
    return dict(value) if isinstance(value, dict) else None


def _openai_delta_text(payload: dict[str, Any]) -> str:
    """Return the text increment of one OpenAI streaming chunk.

    A chunk with no choices is the terminal usage frame, not an error, so it
    yields the empty string. Anything else that is structurally wrong is a
    malformed frame and raises.

    Raises:
        AdapterError: If the first choice or its delta is not an object.
    """
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        raise AdapterError("openai stream choice is not an object")
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    return "" if content is None else str(content)


def _merge_anthropic_usage(
    start: dict[str, Any] | None, delta: dict[str, Any] | None
) -> NormalizedUsage | None:
    """Combine Anthropic's two usage events into one normalized record.

    `message_start` carries the input side and a provisional output count;
    `message_delta` carries the final output count and may repeat or correct the
    input side, so it is merged on top. If either required total is still absent
    the answer is `None` — an unknown count is reported as unknown, never as
    zero.

    Args:
        start: The usage block from `message_start`, if any.
        delta: The usage block from `message_delta`, if any.

    Returns:
        The normalized usage, or `None` when it cannot be determined.
    """
    if start is None and delta is None:
        return None
    merged: dict[str, Any] = {}
    if start is not None:
        merged.update(start)
    if delta is not None:
        merged.update(delta)
    if "input_tokens" not in merged or "output_tokens" not in merged:
        return None
    return normalize_anthropic_usage(merged)


def chunk_text(text: str, size: int) -> tuple[str, ...]:
    """Split `text` into chunks of at most `size` characters.

    Used to turn a buffered completion into wire-sized deltas: the transport
    limit is a property of the link, so splitting belongs here rather than in
    the adapter or the center.

    Args:
        text: The full completion text.
        size: Maximum characters per chunk; must be positive.

    Returns:
        The chunks, in order. Concatenating them reproduces `text`.

    Raises:
        ValueError: If `size` is not positive.
    """
    if size < 1:
        raise ValueError(f"chunk size must be positive, got {size}")
    return tuple(text[index : index + size] for index in range(0, len(text), size)) or ("",)


def probe_endpoint(endpoint: str, *, timeout_s: float = 5.0) -> tuple[bool, str]:
    """Check that an upstream host is reachable, without sending a credential.

    Used by the node's startup self-check so an operator learns about a blocked
    egress path before the center dispatches work to them.

    Args:
        endpoint: The configured upstream URL.
        timeout_s: Connect timeout.

    Returns:
        Tuple of (reachable, detail). The detail is operator-facing and never
        contains a credential, because none is sent.
    """
    parts = urlsplit(endpoint)
    host = parts.hostname
    if not host:
        return False, f"endpoint {endpoint!r} has no host"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True, f"{host}:{port} reachable"
    except OSError as exc:
        return False, f"{host} unreachable: {exc}"
