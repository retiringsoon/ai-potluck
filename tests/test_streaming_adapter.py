"""Tests for the adapter's real streaming path.

Nothing here stubs the transport: a real `http.server.ThreadingHTTPServer` on a
loopback port answers the adapter's POST, and the adapter reads it through
`http.client` exactly as it would read a provider. What is proven is the part
that a buffered `complete` can never exercise — that the request asks for a
stream, that frames are decoded as they arrive even when they are split across
reads, that terminal markers and usage are honoured, and that a broken or
truncated stream is refused instead of being reported as a finished answer.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Callable, Iterable, Iterator
from typing import Any, Literal

import pytest

from aipotluck.adapters import (
    AdapterError,
    AdapterQuotaExceeded,
    HttpUpstreamAdapter,
    SSEFrame,
    iter_sse_events,
)
from aipotluck.wire import ChatMessage, encode_chat_parameters, user_message

#: A responder receives the live request handler and writes the response itself.
Responder = Callable[[http.server.BaseHTTPRequestHandler], None]

#: The single-turn conversation most of these tests run. The adapter is a proxy,
#: so what it posts is asserted against exactly this.
MESSAGES = (user_message("prompt"),)


def _sse(data: str, *, event: str | None = None) -> bytes:
    """Build one SSE frame, optionally with an `event:` name."""
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {data}\n\n".encode()


def _openai_delta(text: str) -> bytes:
    """Build an OpenAI text-increment frame."""
    return _sse(json.dumps({"choices": [{"delta": {"content": text}}]}))


def _openai_usage(prompt_tokens: int, completion_tokens: int) -> bytes:
    """Build OpenAI's terminal usage frame: no choices, a usage block."""
    return _sse(
        json.dumps(
            {
                "choices": [],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            }
        )
    )


OPENAI_DONE = b"data: [DONE]\n\n"


def _anthropic_start(input_tokens: int, cache_read: int = 0, cache_creation: int = 0) -> bytes:
    """Build Anthropic's `message_start`, which carries the input-side usage."""
    return _sse(
        json.dumps(
            {
                "type": "message_start",
                "message": {
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": 1,
                        "cache_read_input_tokens": cache_read,
                        "cache_creation_input_tokens": cache_creation,
                    }
                },
            }
        ),
        event="message_start",
    )


def _anthropic_text(text: str) -> bytes:
    """Build an Anthropic text-delta frame."""
    return _sse(
        json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}),
        event="content_block_delta",
    )


def _anthropic_delta(output_tokens: int) -> bytes:
    """Build Anthropic's `message_delta`, which carries the final output count."""
    return _sse(
        json.dumps(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": output_tokens},
            }
        ),
        event="message_delta",
    )


ANTHROPIC_STOP = _sse(json.dumps({"type": "message_stop"}), event="message_stop")


def _write_sse(handler: http.server.BaseHTTPRequestHandler, pieces: Iterable[bytes]) -> None:
    """Answer with an SSE body, writing each piece and flushing as it goes.

    Pieces are written individually and flushed so the client sees them as they
    become available rather than as one buffered body; a caller that splits an
    SSE frame across two pieces reproduces a frame cut by a TCP read.
    """
    handler.send_response(200)
    handler.send_header("content-type", "text/event-stream")
    handler.send_header("connection", "close")
    handler.end_headers()
    for piece in pieces:
        handler.wfile.write(piece)
        handler.wfile.flush()


class StubServer:
    """A loopback HTTP server that answers every POST with one scripted response."""

    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self.requests: list[dict[str, Any]] = []
        self.errors: list[BaseException] = []

        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                """Silence the default access log."""

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0") or "0")
                raw = self.rfile.read(length)
                try:
                    # `json.loads` rather than the platform's own strict parser:
                    # this records what an upstream would have received, and a
                    # real provider accepts the fractional parameters the
                    # adapter forwards as JSON numbers.
                    body: Any = json.loads(raw) if raw else {}
                except Exception:
                    body = {"__unparseable__": raw.decode("utf-8", "replace")}
                outer.requests.append(
                    {
                        "body": body,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                    }
                )
                try:
                    outer._responder(self)
                except BaseException as exc:
                    outer.errors.append(exc)
                self.close_connection = True

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def url(self) -> str:
        """The `http://` URL the adapter should be pointed at."""
        port = int(self.httpd.server_address[1])
        return f"http://127.0.0.1:{port}/v1/complete"

    @property
    def last_body(self) -> dict[str, Any]:
        """The decoded body of the most recent request."""
        return self.requests[-1]["body"]

    def start(self) -> None:
        """Begin accepting requests."""
        self.thread.start()

    def stop(self) -> None:
        """Stop the server and join its accept thread."""
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5.0)

    def raise_errors(self) -> None:
        """Re-raise the first exception the responder raised, if any."""
        if self.errors:
            raise self.errors[0]


@pytest.fixture
def upstream_server() -> Iterator[Callable[[Responder], StubServer]]:
    """Return a factory that starts a loopback stub upstream per call."""
    servers: list[StubServer] = []

    def start(responder: Responder) -> StubServer:
        server = StubServer(responder)
        server.start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.stop()


def _adapter(url: str, style: Literal["openai", "anthropic"] = "openai") -> HttpUpstreamAdapter:
    """Build an HTTP adapter pointed at a stub server."""
    return HttpUpstreamAdapter(
        endpoint=url, api_key="secret-key", model="stub-model", provider_style=style
    )


# --------------------------------------------------------------------------- #
# Request shape
# --------------------------------------------------------------------------- #


def test_openai_stream_request_asks_for_a_stream_and_usage(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """The streaming call must send `stream:true` and request the usage frame."""
    server = upstream_server(
        lambda handler: _write_sse(handler, [_openai_delta("hi"), _openai_usage(3, 4), OPENAI_DONE])
    )
    adapter = _adapter(server.url)

    chunks = list(adapter.stream(MESSAGES, request_digest="sha256:req", parameters={}))

    server.raise_errors()
    body = server.last_body
    assert body["stream"] is True, "a streaming call must not ask for a buffered body"
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"] == [{"role": "user", "content": "prompt"}]
    assert [chunk.text for chunk in chunks] == ["hi", ""]
    final = chunks[-1]
    assert final.is_final is True
    assert final.usage is not None
    assert (final.usage.input_total, final.usage.output_total) == (3, 4)
    assert all(chunk.usage is None for chunk in chunks[:-1]), (
        "usage belongs to the final chunk only"
    )


def test_complete_still_asks_for_a_buffered_body(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """`complete` must keep sending `stream:false`; the streaming flag is not global."""
    def responder(handler: http.server.BaseHTTPRequestHandler) -> None:
        payload = json.dumps(
            {
                "choices": [{"message": {"content": "done"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            }
        ).encode()
        handler.send_response(200)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(payload)))
        handler.send_header("connection", "close")
        handler.end_headers()
        handler.wfile.write(payload)

    server = upstream_server(responder)
    adapter = _adapter(server.url)

    result = adapter.complete(MESSAGES, request_digest="sha256:req", parameters={})

    server.raise_errors()
    assert server.last_body["stream"] is False
    assert result.text == "done"


def test_anthropic_stream_requests_its_own_body_shape(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """Anthropic streams carry `stream:true` and `max_tokens`, but no OpenAI options."""
    server = upstream_server(
        lambda handler: _write_sse(
            handler, [_anthropic_start(5), _anthropic_text("a"), ANTHROPIC_STOP]
        )
    )
    adapter = _adapter(server.url, style="anthropic")

    list(adapter.stream(MESSAGES, request_digest="sha256:req", parameters={}))

    server.raise_errors()
    body = server.last_body
    assert body["stream"] is True
    assert body["max_tokens"] == 1024
    assert "stream_options" not in body


def test_every_turn_reaches_the_upstream_verbatim(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """A conversation is forwarded whole: no turn dropped, no content rewritten.

    The node is a proxy for one provider call, so the multi-turn context the
    requester paid for has to be the context the model answers from. A
    center-or-adapter that flattened the conversation into one prompt string
    would still return an answer, which is exactly why the assertion is on the
    body the upstream received rather than on the reply.
    """
    conversation = (
        ChatMessage(role="system", content="answer in one word"),
        ChatMessage(role="user", content="what settles a potluck request?"),
        ChatMessage(role="assistant", content="a receipt"),
        ChatMessage(
            role="user",
            content=({"type": "text", "text": "and what backs the receipt?"},),
        ),
    )
    server = upstream_server(
        lambda handler: _write_sse(handler, [_openai_delta("hi"), OPENAI_DONE])
    )
    adapter = _adapter(server.url)

    list(adapter.stream(conversation, request_digest="sha256:req", parameters={}))

    server.raise_errors()
    assert server.last_body["messages"] == [
        {"role": "system", "content": "answer in one word"},
        {"role": "user", "content": "what settles a potluck request?"},
        {"role": "assistant", "content": "a receipt"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "and what backs the receipt?"}],
        },
    ]


def test_forwarded_parameters_reach_the_upstream_as_its_own_types(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """The canonical wire encoding is undone at the upstream boundary.

    Fractions travel as decimal strings, because the platform's canonical
    encoding refuses floats; an upstream that received ``"temperature": "0.7"``
    would reject a request the platform accepted. Everything else keeps the JSON
    type it was forwarded with.
    """
    parameters = encode_chat_parameters(
        {
            "temperature": 0.7,
            "top_p": 1,
            "max_tokens": 64,
            "stop": ["\n", "END"],
            "user": "consumer-X",
            "logit_bias": {"50256": -100},
        }
    )
    assert parameters["temperature"] == "0.7", "a fraction travels as a decimal string"
    server = upstream_server(
        lambda handler: _write_sse(handler, [_openai_delta("hi"), OPENAI_DONE])
    )
    adapter = _adapter(server.url)

    list(adapter.stream(MESSAGES, request_digest="sha256:req", parameters=parameters))

    server.raise_errors()
    body = server.last_body
    assert body["temperature"] == 0.7
    assert body["top_p"] == 1
    assert body["max_tokens"] == 64
    assert body["stop"] == ["\n", "END"]
    assert body["user"] == "consumer-X"
    assert body["logit_bias"] == {"50256": -100}


def test_unlisted_and_null_parameters_are_not_forwarded(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """Only the documented set is forwarded, and an explicit null forwards nothing.

    A parameter the requester has no opinion about must not override the
    upstream's own default with a null, and a field the platform has not decided
    to honour must not become an undocumented way to change what a node asks a
    provider for.
    """
    parameters = encode_chat_parameters(
        {"temperature": None, "n": 3, "logprobs": True, "top_logprobs": 2, "seed": 7}
    )
    assert parameters == {"seed": 7}
    server = upstream_server(
        lambda handler: _write_sse(handler, [_openai_delta("hi"), OPENAI_DONE])
    )
    adapter = _adapter(server.url)

    list(adapter.stream(MESSAGES, request_digest="sha256:req", parameters=parameters))

    server.raise_errors()
    body = server.last_body
    assert body["seed"] == 7
    for absent in ("temperature", "n", "logprobs", "top_logprobs"):
        assert absent not in body


def test_anthropic_system_message_becomes_its_own_field(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """The one reshaping the adapter does is the one Anthropic's API demands.

    Anthropic takes system instructions as a top-level field rather than as a
    turn, so forwarding them verbatim would be a request its API refuses.
    """
    conversation = (
        ChatMessage(role="system", content="answer in one word"),
        ChatMessage(role="user", content="hi"),
    )
    server = upstream_server(
        lambda handler: _write_sse(
            handler, [_anthropic_start(5), _anthropic_text("a"), ANTHROPIC_STOP]
        )
    )
    adapter = _adapter(server.url, style="anthropic")

    list(adapter.stream(conversation, request_digest="sha256:req", parameters={}))

    server.raise_errors()
    body = server.last_body
    assert body["system"] == "answer in one word"
    assert body["messages"] == [{"role": "user", "content": "hi"}]


# --------------------------------------------------------------------------- #
# Frame decoding
# --------------------------------------------------------------------------- #


def test_openai_stream_parses_text_usage_and_done(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """Several text frames, a usage frame, then `[DONE]` combine into one answer."""
    server = upstream_server(
        lambda handler: _write_sse(
            handler,
            [_openai_delta("Hello "), _openai_delta("world"), _openai_usage(800, 200), OPENAI_DONE],
        )
    )
    adapter = _adapter(server.url)

    chunks = list(adapter.stream(MESSAGES, request_digest="sha256:req", parameters={}))

    server.raise_errors()
    assert "".join(chunk.text for chunk in chunks) == "Hello world"
    assert [chunk.is_final for chunk in chunks].count(True) == 1
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 1000


def test_stream_skips_comment_and_heartbeat_lines(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """A `: comment` frame is a keep-alive, not content."""
    server = upstream_server(
        lambda handler: _write_sse(
            handler,
            [b": keep-alive\n\n", _openai_delta("x"), b":\n\n", _openai_usage(1, 1), OPENAI_DONE],
        )
    )
    adapter = _adapter(server.url)

    chunks = list(adapter.stream(MESSAGES, request_digest="sha256:req", parameters={}))

    server.raise_errors()
    assert [chunk.text for chunk in chunks] == ["x", ""]


def test_stream_joins_multiple_data_lines_into_one_frame(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """A frame split over several `data:` lines is one frame, not several."""
    split_frame = (
        b'data: {"choices":[{"delta":{"content":"a"}}],\n'
        b'data: "usage":null}\n\n'
    )
    server = upstream_server(lambda handler: _write_sse(handler, [split_frame, OPENAI_DONE]))
    adapter = _adapter(server.url)

    chunks = list(adapter.stream(MESSAGES, request_digest="sha256:req", parameters={}))

    server.raise_errors()
    assert [chunk.text for chunk in chunks] == ["a", ""]


def test_frames_split_across_reads_are_reassembled(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """Writing the body in tiny pieces must not change the decoded frames.

    The whole body is cut into fixed-size pieces at arbitrary offsets, so a frame
    boundary falls inside a write. The adapter must reassemble on its own buffer.
    """
    whole = _openai_delta("Hello ") + _openai_delta("there") + _openai_usage(10, 5) + OPENAI_DONE
    pieces = [whole[index : index + 7] for index in range(0, len(whole), 7)]
    server = upstream_server(lambda handler: _write_sse(handler, pieces))
    adapter = _adapter(server.url)

    chunks = list(adapter.stream(MESSAGES, request_digest="sha256:req", parameters={}))

    server.raise_errors()
    assert "".join(chunk.text for chunk in chunks) == "Hello there"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 15


def test_anthropic_stream_merges_usage_from_start_and_delta(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """Input usage lives in `message_start`; the final output count in `message_delta`."""
    server = upstream_server(
        lambda handler: _write_sse(
            handler,
            [
                _anthropic_start(100, cache_read=50, cache_creation=10),
                _anthropic_text("Hello "),
                _anthropic_text("world"),
                _anthropic_delta(42),
                ANTHROPIC_STOP,
            ],
        )
    )
    adapter = _adapter(server.url, style="anthropic")

    chunks = list(adapter.stream(MESSAGES, request_digest="sha256:req", parameters={}))

    server.raise_errors()
    assert "".join(chunk.text for chunk in chunks) == "Hello world"
    final = chunks[-1]
    assert final.is_final is True
    assert final.usage is not None
    assert final.usage.input_total == 160, "cache reads and writes are part of the input total"
    assert final.usage.output_total == 42
    assert final.usage.input_cache_read == 50
    assert final.usage.input_cache_write == 10


# --------------------------------------------------------------------------- #
# Parser unit tests (deterministic splitting)
# --------------------------------------------------------------------------- #


def test_iter_sse_events_reassembles_a_frame_split_across_blocks() -> None:
    """A frame cut into three blocks yields exactly one frame."""
    frames = list(iter_sse_events([b'data: {"a"', b":1", b"}\n\n"]))
    assert frames == [SSEFrame(event=None, data='{"a":1}')]


def test_iter_sse_events_ignores_comments_and_dispatches_on_blank_line() -> None:
    """A comment frame dispatches nothing; a blank line ends a frame."""
    frames = list(iter_sse_events([b": ping\n\n", b"data: one\n", b"\n"]))
    assert frames == [SSEFrame(event=None, data="one")]


def test_iter_sse_events_joins_multiple_data_lines() -> None:
    """Multiple `data:` lines are joined with newlines, per the SSE spec."""
    frames = list(iter_sse_events([b"data: first\ndata: second\n\n"]))
    assert frames == [SSEFrame(event=None, data="first\nsecond")]


def test_iter_sse_events_reads_the_event_name() -> None:
    """An `event:` field is carried on the frame."""
    frames = list(iter_sse_events([b"event: message_stop\ndata: {}\n\n"]))
    assert frames == [SSEFrame(event="message_stop", data="{}")]


def test_iter_sse_events_holds_a_utf8_sequence_split_across_blocks() -> None:
    """A multibyte character split across blocks is not decoded as two characters."""
    encoded = "€".encode()
    frames = list(iter_sse_events([b"data: ", encoded[:2], encoded[2:], b"\n\n"]))
    assert frames == [SSEFrame(event=None, data="€")]


def test_iter_sse_events_flushes_a_final_frame_without_a_blank_line() -> None:
    """A trailing frame with data but no final blank line is still surfaced."""
    frames = list(iter_sse_events([b"data: [DONE]"]))
    assert frames == [SSEFrame(event=None, data="[DONE]")]


# --------------------------------------------------------------------------- #
# Failure paths
# --------------------------------------------------------------------------- #


def test_malformed_frame_is_rejected(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """A frame whose data is not strict JSON is refused, not skipped."""
    server = upstream_server(
        lambda handler: _write_sse(handler, [b"data: {not json}\n\n", OPENAI_DONE])
    )
    adapter = _adapter(server.url)

    with pytest.raises(AdapterError):
        list(adapter.stream(MESSAGES, request_digest="sha256:req", parameters={}))


def test_openai_stream_truncated_before_done_raises(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """Closing after some text but before `[DONE]` is a failure, not a short answer."""
    server = upstream_server(lambda handler: _write_sse(handler, [_openai_delta("Hello ")]))
    adapter = _adapter(server.url)

    collected: list[str] = []
    with pytest.raises(AdapterError):
        for chunk in adapter.stream(MESSAGES, request_digest="sha256:req", parameters={}):
            collected.append(chunk.text)
    assert "".join(collected) == "Hello ", "the text already produced is not lost"


def test_anthropic_stream_truncated_before_message_stop_raises(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """Closing before `message_stop` is a failure, not a completed answer."""
    server = upstream_server(
        lambda handler: _write_sse(handler, [_anthropic_start(1), _anthropic_text("Hi")])
    )
    adapter = _adapter(server.url, style="anthropic")

    with pytest.raises(AdapterError):
        list(adapter.stream(MESSAGES, request_digest="sha256:req", parameters={}))


def test_anthropic_error_event_is_rejected(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """An explicit `error` event surfaces as an adapter error."""
    error_frame = _sse(
        json.dumps({"type": "error", "error": {"message": "overloaded"}}), event="error"
    )
    server = upstream_server(lambda handler: _write_sse(handler, [error_frame]))
    adapter = _adapter(server.url, style="anthropic")

    with pytest.raises(AdapterError):
        list(adapter.stream(MESSAGES, request_digest="sha256:req", parameters={}))


def test_stream_reports_quota_exhaustion(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """HTTP 429 on the streaming call is a quota error, not a generic failure."""

    def responder(handler: http.server.BaseHTTPRequestHandler) -> None:
        handler.send_response(429)
        handler.send_header("content-length", "0")
        handler.send_header("connection", "close")
        handler.end_headers()

    server = upstream_server(responder)
    adapter = _adapter(server.url)

    with pytest.raises(AdapterQuotaExceeded):
        list(adapter.stream(MESSAGES, request_digest="sha256:req", parameters={}))


def test_stream_reports_other_http_errors(
    upstream_server: Callable[[Responder], StubServer],
) -> None:
    """A non-2xx, non-429 status is a generic adapter error."""

    def responder(handler: http.server.BaseHTTPRequestHandler) -> None:
        handler.send_response(500)
        handler.send_header("content-length", "0")
        handler.send_header("connection", "close")
        handler.end_headers()

    server = upstream_server(responder)
    adapter = _adapter(server.url)

    with pytest.raises(AdapterError):
        list(adapter.stream(MESSAGES, request_digest="sha256:req", parameters={}))


def test_unreachable_upstream_is_unavailable() -> None:
    """A closed port is reported as unreachable before any frame is read."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    adapter = _adapter(f"http://127.0.0.1:{port}/v1/complete")

    with pytest.raises(AdapterError):
        list(adapter.stream(MESSAGES, request_digest="sha256:req", parameters={}))
