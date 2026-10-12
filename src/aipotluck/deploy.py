"""Operator-facing deployment, contributor and consumer side: configuration
schemas, node/consumer loading, and the consumer HTTP client.

Two JSON schemas drive the two client roles. Every file is parsed with
:func:`aipotluck.canonical.loads_strict`, so a float, a repeated key, or a
``NaN`` literal is refused at the boundary — the same rules the wire applies,
because a config that only mostly parses is a config an operator cannot audit.

``node.json`` — one contribution node process::

    {
      "center_url": "wss://center.example/v1/node-link",
      "node_id": "node-A", "device_id": "device-A",
      "device_key_id": "device-key-A", "provider_id": "provider-A",
      "database_path": "node.db", "token": "...",
      "adapter": {"kind": "http",
                  "endpoint": "https://api.example/v1/chat/completions",
                  "api_key": "...", "model": "m", "provider_style": "openai",
                  "name": "main"},
      "device_private_key_b64": "<base64 Ed25519 private key>",
      "share_enabled": true, "max_concurrency": 1,
      "max_local_reserved_milli_credits": 20000
    }

``consumer.json`` — one consumer's connection settings::

    {"center_url": "https://center.example", "token": "..."}

The center's own configuration (``center.json``), its provisioning and its
inspection handles live in ``ai-potluck-server``; the shared config-parsing
helpers below are public because that package builds on them.

Secrecy rules
-------------
* A config file's tokens are only ever carried in an ``Authorization`` header.
  No loader logs, and no error message raised here quotes a token value:
  validation messages name fields, not values, and the HTTP helpers scrub the
  consumer token from any server-rendered error body before raising.
* ``node.json``'s upstream ``api_key`` exists only inside the
  `HttpUpstreamAdapter` it builds. `redacted_node` is the supported way to
  render a node configuration for diagnostics.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import http.client
import queue
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from aipotluck.proxy import http_connection_for
from aipotluck.tls import client_ssl_context

from .adapters import Adapter, FakeUpstreamAdapter, HttpUpstreamAdapter
from .canonical import canonicalize, loads_strict, sign_payload
from .connector import ConnectorConfig
from .connector import redact as redact_connector_config
from .errors import CanonicalizationError, PotluckError
from .identity import load_private_key
from .records import (
    DOMAIN_REQUEST_AUTH,
    REQUEST_AUTH_SCHEME,
    format_request_auth_header,
    request_auth_payload,
    request_body_digest,
)
from .statemachine import TERMINAL_REQUEST_STATES

#: The instant every provisioned binding and float is dated to. `is_valid_at`
#: refuses any record dated before its key's registration, so registering at
#: ``now()`` would make a deployment fail the first time an operation carried a
#: fixed past timestamp; a fixed past epoch keeps provisioning hermetic and
#: re-provisioning a no-op across restarts.
PROVISIONED_AT = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)

#: Connect/read timeout for one consumer HTTP round trip.
CONSUMER_HTTP_TIMEOUT_S = 30.0

#: How long `consumer_events` waits for the terminal ``done`` frame before
#: giving up. A request that is still running keeps the SSE stream open; the
#: client stops rather than block forever.
EVENT_STREAM_TIMEOUT_S = 30.0

#: How long the ``request --wait`` poll loop waits for a terminal status.
REQUEST_WAIT_TIMEOUT_S = 120.0


_ADAPTER_KINDS = frozenset({"http", "fake"})
_PROVIDER_STYLES = frozenset({"openai", "anthropic"})


class DeploymentError(PotluckError):
    """Raised when a deployment configuration or a consumer call is unusable.

    The message is operator-facing and actionable. It never quotes a token or
    an upstream api_key: validation failures name the offending field, and
    server-rendered error bodies are scrubbed against the caller's own token.

    A response failure also records the structured facts — :attr:`http_status`
    and :attr:`error_code` — so a caller can decide what to advise
    (:func:`aipotluck.ux.classify_failure`) without parsing a message. They are
    plain attributes rather than constructor fields because the many
    validation-time raises build the error from a single string, and threading
    two keyword arguments through every one of them would add noise without
    adding information.
    """

    #: HTTP status of the failed response, or `None` for a local/transport error.
    http_status: int | None = None
    #: The gateway's machine code (its ``error`` field), when one was returned.
    error_code: str | None = None


@dataclass(frozen=True)

class SseFrame:
    """One frame read off the gateway's server-sent event stream."""

    #: The stored event cursor (the SSE ``id`` field), or `None` for frames the
    #: gateway generates, such as the terminal ``done`` frame.
    cursor: int | None
    event: str
    payload: dict[str, Any]


#: How long `LiveEventStream.close` waits for its reader thread to notice the
#: closed socket. The shutdown should wake it immediately; the timeout is only
#: a guard so a wedged read can never hang the consumer's exit path.
_STREAM_CLOSE_JOIN_S = 2.0

#: Sentinel the reader thread queues last, so the consumer can tell "the stream
#: ended" from "no frame right now".
_STREAM_END: Any = object()


class LiveEventStream:
    """A live SSE read of one request's events, consumable without blocking.

    Why a reader thread rather than a short socket timeout: ``http.client``
    cannot resume a read after a socket timeout — once a read times out the
    response object refuses further reads ("cannot read from timed out object",
    measured, not assumed). So the socket is read blocking on a daemon thread
    and frames are handed to the consumer through a queue. The consumer can then
    poll with its own deadline, react to Ctrl-C, and drop the stream, while the
    reader is unblocked by shutting the socket down (a plain ``close`` does not
    reliably wake a blocked ``recv`` on every platform; ``shutdown`` does).

    The thread owns the socket; `close` is the only method other threads call to
    end a read early. `poll` is the only consumer entry point, and it never
    blocks longer than its timeout.
    """

    def __init__(
        self,
        *,
        request_id: str,
        cursor: int,
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
    ) -> None:
        """Record the open response and prepare the reader thread.

        Args:
            request_id: The request being streamed, for error messages.
            cursor: The highest cursor already seen; the server replays later
                events only, which is what makes a reconnect duplicate-free.
            connection: The owning connection, shut down to abort the read.
            response: The already-open SSE response to read frames from.
        """
        self._request_id = request_id
        self._cursor = cursor
        self._connection = connection
        self._response = response
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name=f"potluck-sse-{request_id}", daemon=True
        )
        self._ended = False
        self._error: DeploymentError | None = None
        self._closed = False
        self._done = False

    @property
    def request_id(self) -> str:
        """Return the request this stream reads."""
        return self._request_id

    @property
    def start_cursor(self) -> int:
        """Return the cursor this stream started from."""
        return self._cursor

    @property
    def ended(self) -> bool:
        """Whether the stream has ended (cleanly, by error, or by close)."""
        return self._ended

    @property
    def saw_done(self) -> bool:
        """Whether the server sent its terminal ``done`` frame on this stream."""
        return self._done

    @property
    def error(self) -> DeploymentError | None:
        """Return the terminal stream error, if the reader hit one."""
        return self._error

    def start(self) -> None:
        """Start the reader thread, once."""
        self._thread.start()

    def _run(self) -> None:
        """Read frames on the reader thread and hand them to the queue."""
        try:
            for frame in _iter_sse_frames(self._response, request_id=self._request_id):
                self._queue.put(frame)
                if frame.event == "done":
                    self._done = True
                    return
                if self._closed:
                    return
        except DeploymentError as exc:
            self._error = exc
        except OSError as exc:  # the socket was shut down, or the peer vanished
            self._error = DeploymentError(
                f"the event stream for request {self._request_id} ended early: {exc}"
            )
        except Exception as exc:  # never let a reader thread die silently
            self._error = DeploymentError(
                f"the event stream for request {self._request_id} failed: {exc!r}"
            )
        finally:
            self._queue.put(_STREAM_END)

    def poll(self, timeout_s: float) -> SseFrame | None:
        """Return the next frame, or `None` when the stream is idle.

        Args:
            timeout_s: Longest time to wait for a frame.

        Returns:
            The next frame, or `None` when none arrived within `timeout_s` or
            the stream has ended. Callers check :attr:`ended` /
            :attr:`saw_done` / :attr:`error` to tell those apart.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                if self._ended:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    item = self._queue.get(timeout=remaining)
                except queue.Empty:
                    return None
            if item is _STREAM_END:
                self._ended = True
                continue
            if isinstance(item, DeploymentError):
                self._error = item
                self._ended = True
                continue
            return cast("SseFrame", item)

    def close(self) -> None:
        """Abort the read and release the connection.

        Shutting the socket down wakes a reader parked in ``recv`` immediately,
        so a consumer that has decided to stop (deadline, Ctrl-C, reconnect) is
        not held up by the stream it is abandoning.
        """
        self._closed = True
        sock = getattr(self._connection, "sock", None)
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self._connection.close()
        if self._thread.is_alive():
            self._thread.join(timeout=_STREAM_CLOSE_JOIN_S)



# --------------------------------------------------------------------------
# Strict document field extraction. Every message names fields, never values,
# so a secret that happened to sit in a refused document cannot leak through
# an error message.
# --------------------------------------------------------------------------


def load_document(path: Path) -> dict[str, Any]:
    """Read and strict-parse one configuration file.

    Raises:
        DeploymentError: If the file cannot be read, is not strict JSON (a
            float, a duplicate key, or ``NaN`` refuses the whole file), or is
            not a JSON object at the top level.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DeploymentError(f"configuration file not found: {path}") from exc
    except IsADirectoryError as exc:
        raise DeploymentError(f"configuration path is a directory, not a file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise DeploymentError(f"configuration file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise DeploymentError(f"configuration file could not be read: {path}: {exc}") from exc
    try:
        parsed = loads_strict(text)
    except CanonicalizationError as exc:
        raise DeploymentError(f"configuration file {path} was refused: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DeploymentError(
            f"configuration file {path} must contain a JSON object at the top level"
        )
    return cast("dict[str, Any]", parsed)


def _field(document: dict[str, Any], key: str, context: str) -> Any:
    """Return a required field's raw value.

    Raises:
        DeploymentError: If the field is absent.
    """
    if key not in document:
        raise DeploymentError(f"{context}: required field {key!r} is missing")
    return document[key]


def require_str(document: dict[str, Any], key: str, context: str) -> str:
    """Return a required non-empty string field.

    Raises:
        DeploymentError: If the field is absent, empty, or not a string.
    """
    value = _field(document, key, context)
    if not isinstance(value, str) or not value:
        raise DeploymentError(f"{context}: field {key!r} must be a non-empty string")
    return value


def str_or_none(document: dict[str, Any], key: str, context: str) -> str | None:
    """Return a string-or-null field.

    Raises:
        DeploymentError: If the field is present but neither a string nor null.
    """
    value = document.get(key)
    if value is None or isinstance(value, str):
        return value
    raise DeploymentError(f"{context}: field {key!r} must be a string or null")


def require_int(document: dict[str, Any], key: str, context: str) -> int:
    """Return a required integer field.

    ``bool`` is refused explicitly: it is an `int` subclass in Python, and
    ``true`` is not a port or an amount.

    Raises:
        DeploymentError: If the field is absent or not an integer.
    """
    value = _field(document, key, context)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeploymentError(f"{context}: field {key!r} must be an integer")
    return value


def require_bool(document: dict[str, Any], key: str, context: str) -> bool:
    """Return a required boolean field.

    Raises:
        DeploymentError: If the field is absent or not a boolean.
    """
    value = _field(document, key, context)
    if not isinstance(value, bool):
        raise DeploymentError(f"{context}: field {key!r} must be a boolean")
    return value


def optional_bool(document: dict[str, Any], key: str, context: str, default: bool) -> bool:
    """Return an optional boolean field, or `default` when it is absent.

    Absent and malformed are different facts and get different answers. A field
    an operator never wrote takes the documented default; a field they wrote
    wrong is refused at load, because a config that silently reads ``"on"`` as
    false is a config whose behaviour nobody can audit from the file.

    Raises:
        DeploymentError: If the field is present but not a boolean.
    """
    if key not in document:
        return default
    value = document[key]
    if not isinstance(value, bool):
        raise DeploymentError(f"{context}: field {key!r} must be a boolean")
    return value


def optional_int(document: dict[str, Any], key: str, context: str, default: int) -> int:
    """Return an optional integer field, or `default` when it is absent.

    ``bool`` is refused explicitly, for the reason `require_int` refuses it: it
    is an `int` subclass in Python, and ``true`` is not a count or an amount.

    Raises:
        DeploymentError: If the field is present but not an integer.
    """
    if key not in document:
        return default
    value = document[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeploymentError(f"{context}: field {key!r} must be an integer")
    return value


def require_list(document: dict[str, Any], key: str, context: str) -> list[Any]:
    """Return a required list field.

    Raises:
        DeploymentError: If the field is absent or not a list.
    """
    value = _field(document, key, context)
    if not isinstance(value, list):
        raise DeploymentError(f"{context}: field {key!r} must be a list")
    return value


def require_object_list(document: dict[str, Any], key: str, context: str) -> list[dict[str, Any]]:
    """Return a required list-of-objects field.

    Raises:
        DeploymentError: If the field is absent, not a list, or holds a
            non-object entry.
    """
    entries = require_list(document, key, context)
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise DeploymentError(
                f"{context}: {key}[{index}] must be a JSON object, "
                f"got {type(entry).__name__}"
            )
    return cast("list[dict[str, Any]]", entries)


def absolute_url(url: str, *, what: str, schemes: tuple[str, ...], context: str) -> None:
    """Refuse a non-absolute URL of the expected schemes.

    Raises:
        DeploymentError: If the URL has no recognized scheme or no host.
    """
    parts = urlsplit(url)
    if parts.scheme not in schemes or not parts.hostname:
        expected = " or ".join(f"{scheme}://" for scheme in schemes)
        raise DeploymentError(
            f"{context}: {what} must be an absolute URL starting with {expected}, "
            f"not {url!r}"
        )



# --------------------------------------------------------------------------
# Node and consumer configuration
# --------------------------------------------------------------------------


def _load_adapter(document: dict[str, Any], context: str) -> Adapter:
    """Build the one fixed upstream adapter a node configures.

    Raises:
        DeploymentError: If the adapter block is malformed, an ``http`` adapter
            has a missing or non-absolute endpoint, an ``http`` adapter has no
            api_key, or a ``fake`` adapter was handed an api_key. A fake
            upstream has no credential, so silently accepting one would let an
            operator believe their key is being used when it is not.
    """
    adapter_context = f"{context}: adapter"
    if not isinstance(document.get("adapter"), dict):
        raise DeploymentError(f"{adapter_context} must be a JSON object")
    adapter = cast("dict[str, Any]", document["adapter"])
    kind = require_str(adapter, "kind", adapter_context)
    if kind not in _ADAPTER_KINDS:
        raise DeploymentError(
            f"{adapter_context}: field 'kind' must be one of {sorted(_ADAPTER_KINDS)}, "
            f"not {kind!r}"
        )
    endpoint = str_or_none(adapter, "endpoint", adapter_context)
    api_key = str_or_none(adapter, "api_key", adapter_context)
    model = require_str(adapter, "model", adapter_context)
    provider_style = require_str(adapter, "provider_style", adapter_context)
    if provider_style not in _PROVIDER_STYLES:
        raise DeploymentError(
            f"{adapter_context}: field 'provider_style' must be one of "
            f"{sorted(_PROVIDER_STYLES)}, not {provider_style!r}"
        )
    name = require_str(adapter, "name", adapter_context)
    max_tokens = 1024
    if adapter.get("max_tokens") is not None:
        raw = adapter["max_tokens"]
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
            raise DeploymentError(
                f"{adapter_context}: field 'max_tokens' must be a positive integer"
            )
        max_tokens = raw
    if kind == "http":
        if endpoint is None:
            raise DeploymentError(
                f"{adapter_context}: kind \"http\" requires an 'endpoint'; the node "
                "executes against one fixed upstream, and there is nothing to execute "
                "against without it"
            )
        absolute_url(
            endpoint, what="'endpoint'", schemes=("http", "https"), context=adapter_context
        )
        if api_key is None:
            raise DeploymentError(
                f"{adapter_context}: kind \"http\" requires an 'api_key'; the upstream "
                "credential is operator configuration and lives only inside the adapter"
            )
        # `HttpUpstreamAdapter` is frozen, so its `name`/`model` are read-only
        # while the `Adapter` protocol declares settable attributes; the
        # adapter never needs them reassigned, and the cast documents that.
        return cast(
            Adapter,
            HttpUpstreamAdapter(
                endpoint=endpoint,
                api_key=api_key,
                model=model,
                provider_style=cast("Literal['openai', 'anthropic']", provider_style),
                name=name,
                max_tokens=max_tokens,
            ),
        )
    if api_key is not None:
        raise DeploymentError(
            f"{adapter_context}: kind \"fake\" must not carry an 'api_key'; a fake "
            "upstream has no credential, so the key would silently go unused while "
            "the operator believes it is being sent — remove 'api_key' or switch "
            "kind to \"http\""
        )
    return FakeUpstreamAdapter(name=name, model=model)


def load_node(path: str | Path) -> tuple[ConnectorConfig, Adapter]:
    """Load a node config and build its adapter.

    The upstream ``api_key`` exists only inside the returned adapter; the
    `ConnectorConfig` carries no upstream credential. `redacted_node` is the
    supported way to render either for diagnostics.

    Args:
        path: The ``node.json`` file.

    Returns:
        The connector configuration and the configured adapter.

    Raises:
        DeploymentError: On any malformed field, a non-``ws(s)://`` center
            URL, or a refused adapter block.
    """
    config_path = Path(path)
    context = f"node configuration {config_path}"
    document = load_document(config_path)
    center_url = require_str(document, "center_url", context)
    absolute_url(center_url, what="'center_url'", schemes=("ws", "wss"), context=context)
    node_id = require_str(document, "node_id", context)
    device_id = require_str(document, "device_id", context)
    device_key_id = require_str(document, "device_key_id", context)
    provider_id = require_str(document, "provider_id", context)
    database_path = require_str(document, "database_path", context)
    token = require_str(document, "token", context)
    device_private_key_b64 = require_str(document, "device_private_key_b64", context)
    share_enabled = require_bool(document, "share_enabled", context)
    max_concurrency = require_int(document, "max_concurrency", context)
    if max_concurrency < 1:
        raise DeploymentError(f"{context}: field 'max_concurrency' must be at least 1")
    max_reserved = require_int(document, "max_local_reserved_milli_credits", context)
    if max_reserved < 0:
        raise DeploymentError(
            f"{context}: field 'max_local_reserved_milli_credits' must be non-negative"
        )
    adapter = _load_adapter(document, context)
    config = ConnectorConfig(
        center_url=center_url,
        node_id=node_id,
        device_id=device_id,
        device_key_id=device_key_id,
        provider_id=provider_id,
        database_path=database_path,
        token=token,
        adapter_name=adapter.name,
        model=adapter.model,
        device_private_key_b64=device_private_key_b64,
        share_enabled=share_enabled,
        max_concurrency=max_concurrency,
        max_local_reserved_milli_credits=max_reserved,
        # The path is handed to the connector so `share pause`/`resume` can flip
        # the switch in this very file and have a running node pick it up. It is
        # a path, not the document: the connector re-reads only the share
        # fields, so the upstream credential never leaves the adapter.
        config_path=str(config_path),
    )
    return config, adapter


def redacted_node(config: ConnectorConfig, adapter: Adapter) -> dict[str, object]:
    """Return a log-safe summary of a node configuration.

    The platform token and the device private key are masked through
    `connector.redact`; the upstream credential never enters the summary,
    because `HttpUpstreamAdapter.redacted` reports only host, path, model and
    provider style — and the fake adapter holds no credential at all.
    """
    summary: dict[str, object] = dict(redact_connector_config(config))
    if isinstance(adapter, HttpUpstreamAdapter):
        summary["upstream"] = adapter.redacted()
    else:
        summary["upstream"] = {"adapter": adapter.name, "model": adapter.model}
    return summary


def load_consumer(path: str | Path) -> dict[str, str]:
    """Load consumer connection settings.

    Args:
        path: The ``consumer.json`` file, holding ``center_url`` (http or
            https) and the consumer ``token``.

    Returns:
        ``{"center_url": ..., "token": ...}``. No ``redacted()`` summary
        exists for this mapping on purpose: it *is* the secret's only carrier,
        it is never printed, and every diagnostic goes through
        `consumer_get`/`consumer_post`, which scrub the token from anything
        they raise.

    Raises:
        DeploymentError: If the file is malformed or the URL is not an
            absolute ``http(s)://`` URL.
    """
    config_path = Path(path)
    context = f"consumer configuration {config_path}"
    document = load_document(config_path)
    center_url = require_str(document, "center_url", context)
    absolute_url(center_url, what="'center_url'", schemes=("http", "https"), context=context)
    token = require_str(document, "token", context)
    return {"center_url": center_url, "token": token}


# --------------------------------------------------------------------------
# Consumer HTTP client. The token appears only in the Authorization header;
# every raised message is scrubbed against it.
# --------------------------------------------------------------------------


def _scrub_token(text: str, token: str) -> str:
    """Replace the consumer token in `text` with a placeholder."""
    if token and token in text:
        return text.replace(token, "<redacted>")
    return text


def _consumer_parts(consumer: dict[str, str]) -> tuple[Any, str, int]:
    """Split a consumer's center URL into connection parts.

    Raises:
        DeploymentError: If the URL is not an absolute http(s) URL. (Config
            already validated it; the guard keeps a hand-built mapping honest.)
    """
    parts = urlsplit(consumer["center_url"])
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise DeploymentError(
            f"center_url must be an absolute http:// or https:// URL, "
            f"not {consumer['center_url']!r}"
        )
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, str(parts.hostname), port


def _error_body(raw: bytes, token: str) -> str:
    """Render a non-2xx response body for an error message, token scrubbed."""
    try:
        parsed = loads_strict(raw)
    except PotluckError:
        text = raw.decode("utf-8", "replace")
    else:
        if isinstance(parsed, dict) and isinstance(parsed.get("detail"), str):
            text = parsed["detail"]
        else:
            text = raw.decode("utf-8", "replace")
    return _scrub_token(text[:500], token)


def _error_code(raw: bytes) -> str | None:
    """Extract the gateway's machine code from a non-2xx body, if it has one.

    The gateway answers errors as ``{"error": <code>, "detail": ...}``. The code
    is a structured claim about *why*, which is what lets the CLI pick the right
    next step instead of pattern-matching the human detail.
    """
    try:
        parsed = loads_strict(raw)
    except PotluckError:
        return None
    if isinstance(parsed, dict):
        code = parsed.get("error")
        if isinstance(code, str) and code:
            return code
    return None


def _http_failure(
    method: str, target: str, status: int, raw: bytes, token: str
) -> DeploymentError:
    """Build a `DeploymentError` for a non-2xx response, with structured facts."""
    error = DeploymentError(
        f"{method} {target} failed with HTTP {status}: {_error_body(raw, token)}"
    )
    error.http_status = status
    error.error_code = _error_code(raw)
    return error


def _consumer_request(
    consumer: dict[str, str], method: str, path: str, body: dict[str, Any] | None
) -> dict[str, Any]:
    """Run one authenticated consumer round trip against the center.

    The request body is sent in canonical form, so a client-side bug cannot
    put a float on the wire the gateway would refuse anyway. The token is sent
    only as a bearer header; it never appears in a raised message.

    Raises:
        DeploymentError: On a non-2xx answer (with the server's error body,
            scrubbed) or an unusable response.
    """
    scheme, host, port = _consumer_parts(consumer)
    token = consumer["token"]
    payload = canonicalize(body) if body is not None else None
    headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
    if payload is not None:
        headers["content-type"] = "application/json"
        headers["content-length"] = str(len(payload))
    connection = http_connection_for(
        scheme, host, port, timeout_s=CONSUMER_HTTP_TIMEOUT_S,
        tls_context=client_ssl_context(),
    )
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
    except OSError as exc:
        raise DeploymentError(
            f"{method} {path} could not reach the center at {host}:{port}: {exc}"
        ) from exc
    finally:
        connection.close()
    if not 200 <= response.status < 300:
        raise _http_failure(method, path, response.status, raw, token)
    try:
        parsed = loads_strict(raw)
    except PotluckError as exc:
        raise DeploymentError(
            f"{method} {path} returned a non-JSON body with HTTP {response.status}"
        ) from exc
    if not isinstance(parsed, dict):
        raise DeploymentError(
            f"{method} {path} returned HTTP {response.status} with a non-object body"
        )
    return cast("dict[str, Any]", parsed)


def bearer_get(center_url: str, token: str, path: str) -> dict[str, Any]:
    """Authenticated ``GET`` against a center, raising on non-2xx.

    This is the role-agnostic form of `consumer_get`. A contribution node holds a
    platform token exactly as a provisioned consumer does, and the read-only
    endpoint it reconciles against answers to the same ``Authorization: Bearer``
    header, so both share one request path and one scrubbing rule rather than
    each growing its own.

    Args:
        center_url: Absolute ``http(s)://`` URL of the center.
        token: The bearer credential. It is sent only in the header and is
            scrubbed from every raised message.
        path: The endpoint path, e.g. ``/v1/node/attempts``.

    Returns:
        The parsed JSON object the center answered with.

    Raises:
        DeploymentError: On any transport or HTTP failure.
    """
    return _consumer_request({"center_url": center_url, "token": token}, "GET", path, None)


def consumer_get(consumer: dict[str, str], path: str) -> dict[str, Any]:
    """Authenticated ``GET`` against the center, raising on non-2xx.

    The error message carries the server's error body with the caller's token
    scrubbed; the token is never logged, because nothing here logs at all.

    Raises:
        DeploymentError: On any transport or HTTP failure.
    """
    return bearer_get(consumer["center_url"], consumer["token"], path)


def consumer_post(consumer: dict[str, str], path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Authenticated canonical-JSON ``POST`` against the center.

    Raises:
        DeploymentError: On any transport or HTTP failure, with the server's
            error body rendered and scrubbed of the caller's token.
    """
    return _consumer_request(consumer, "POST", path, body)


def consumer_events(
    consumer: dict[str, str],
    request_id: str,
    cursor: int,
    *,
    timeout_s: float = EVENT_STREAM_TIMEOUT_S,
) -> list[SseFrame]:
    """Read the stored events of one request from the gateway's event stream.

    Which transport, and why: the gateway answers
    ``GET /v1/requests/{id}/events`` with ``text/event-stream`` only — this
    build offers no plain-JSON variant of the endpoint (the server's route
    table maps the path straight to its SSE handler) — so the client side is a
    short SSE read. It parses the replayed frames, keeps the heartbeat
    comments out of the result, and returns as soon as the gateway's terminal
    ``done`` frame arrives, which the server sends once the request has
    reached a terminal state. A request that is still running keeps the stream
    open past `timeout_s`; that is reported as an error rather than blocking
    the operator forever. A server that closes the stream early returns the
    frames read so far, because they are real stored events.

    Args:
        consumer: Connection settings from `load_consumer`.
        request_id: The request whose events to read.
        cursor: Highest cursor already seen; only later events are replayed.
        timeout_s: Socket timeout for the whole read.

    Returns:
        The frames in stream order, ending with the ``done`` frame when the
        request was already terminal.

    Raises:
        DeploymentError: On a non-2xx answer, an unparseable frame, a
            ``dropped`` frame, or the timeout elapsing without a terminal
            frame.
    """
    token = consumer["token"]
    scheme, host, port = _consumer_parts(consumer)
    path = f"/v1/requests/{request_id}/events?cursor={cursor}"
    connection = http_connection_for(
        scheme, host, port, timeout_s=timeout_s,
        tls_context=client_ssl_context(),
    )
    try:
        connection.request(
            "GET",
            path,
            headers={
                "authorization": f"Bearer {token}",
                "accept": "text/event-stream",
            },
        )
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raw = response.read()
            raise _http_failure("GET", path, response.status, raw, token)
        return _read_sse_frames(response, request_id=request_id)
    except OSError as exc:
        raise DeploymentError(
            f"the event stream for request {request_id} produced no terminal frame "
            f"within {timeout_s:g}s (the request may still be running): {exc}"
        ) from exc
    finally:
        connection.close()


@dataclass(frozen=True)
class SignedClient:
    """A consumer that authenticates with ``AIPotluck-Sig`` instead of a token.

    This is the client half of the per-request signature: it holds the private
    key a joiner generated and signs each request's canonical payload — method,
    path, query, body digest, key id, timestamp and a fresh nonce — so the
    center can resolve the identity from the registry rather than a
    pre-provisioned token. It is deliberately separate from the bearer `consumer_*`
    helpers: those carry a token, this carries a key.

    The key material is loaded from `private_key_b64` on each call and is never
    rendered; every raised message is scrubbed of it the same way the bearer
    helpers scrub their token.
    """

    center_url: str
    key_id: str
    private_key_b64: str

    def request(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        body: dict[str, Any] | None = None,
        timeout_s: float = CONSUMER_HTTP_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Sign and send one request, returning the parsed JSON answer.

        Raises:
            DeploymentError: On a non-2xx answer (with the server's scrubbed
                error body) or an unusable response.
        """
        private_key = load_private_key(self.private_key_b64)
        issued_at = dt.datetime.now(dt.UTC)
        nonce = secrets.token_urlsafe(24)
        body_bytes = canonicalize(body) if body is not None else None
        digest = request_body_digest(body_bytes)
        payload = request_auth_payload(
            method=method,
            path=path,
            query=query,
            body_digest=digest,
            key_id=self.key_id,
            issued_at=issued_at,
            nonce=nonce,
        )
        signature = sign_payload(DOMAIN_REQUEST_AUTH, payload, private_key)
        header = format_request_auth_header(
            key_id=self.key_id, issued_at=issued_at, nonce=nonce, signature_b64=signature
        )
        scheme, host, port = _consumer_parts({"center_url": self.center_url})
        target = f"{path}?{query}" if query else path
        headers = {
            "authorization": f"{REQUEST_AUTH_SCHEME} {header}",
            "accept": "application/json",
        }
        if body_bytes is not None:
            headers["content-type"] = "application/json"
            headers["content-length"] = str(len(body_bytes))
        connection = http_connection_for(
            scheme, host, port, timeout_s=timeout_s,
            tls_context=client_ssl_context(),
        )
        try:
            connection.request(method, target, body=body_bytes, headers=headers)
            response = connection.getresponse()
            raw = response.read()
        except OSError as exc:
            raise DeploymentError(
                f"{method} {target} could not reach the center at {host}:{port}: {exc}"
            ) from exc
        finally:
            connection.close()
        if not 200 <= response.status < 300:
            raise _http_failure(method, target, response.status, raw, "")
        try:
            parsed = loads_strict(raw)
        except PotluckError as exc:
            raise DeploymentError(
                f"{method} {target} returned a non-JSON body with HTTP {response.status}"
            ) from exc
        if not isinstance(parsed, dict):
            raise DeploymentError(
                f"{method} {target} returned HTTP {response.status} with a non-object body"
            )
        return cast("dict[str, Any]", parsed)

    def get(self, path: str, *, query: str = "") -> dict[str, Any]:
        """Signed ``GET`` against the center."""
        return self.request("GET", path, query=query)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Signed canonical-JSON ``POST`` against the center."""
        return self.request("POST", path, body=body)

    def _open_event_response(
        self, request_id: str, cursor: int, *, timeout_s: float
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        """Sign, send and open a request's SSE stream, returning the live pair.

        Split out so the buffered reader (`events`) and the live reader
        (`open_event_stream`) share one signing and one status-check path: two
        copies of the ``AIPotluck-Sig`` construction is exactly the kind of drift
        that makes one of them stop authenticating after a header change.

        Raises:
            DeploymentError: On a non-2xx answer or a transport failure. The
                connection is closed before the error escapes.
        """
        private_key = load_private_key(self.private_key_b64)
        issued_at = dt.datetime.now(dt.UTC)
        nonce = secrets.token_urlsafe(24)
        path = f"/v1/requests/{request_id}/events"
        query = f"cursor={cursor}"
        payload = request_auth_payload(
            method="GET",
            path=path,
            query=query,
            body_digest=request_body_digest(None),
            key_id=self.key_id,
            issued_at=issued_at,
            nonce=nonce,
        )
        signature = sign_payload(DOMAIN_REQUEST_AUTH, payload, private_key)
        header = format_request_auth_header(
            key_id=self.key_id, issued_at=issued_at, nonce=nonce, signature_b64=signature
        )
        scheme, host, port = _consumer_parts({"center_url": self.center_url})
        connection = http_connection_for(
            scheme, host, port, timeout_s=timeout_s,
            tls_context=client_ssl_context(),
        )
        try:
            connection.request(
                "GET",
                f"{path}?{query}",
                headers={
                    "authorization": f"{REQUEST_AUTH_SCHEME} {header}",
                    "accept": "text/event-stream",
                },
            )
            response = connection.getresponse()
        except OSError as exc:
            connection.close()
            raise DeploymentError(
                f"could not reach the center for the event stream of request "
                f"{request_id}: {exc}"
            ) from exc
        if not 200 <= response.status < 300:
            raw = response.read()
            failure = _http_failure("GET", path, response.status, raw, "")
            connection.close()
            raise failure
        return connection, response

    def events(
        self, request_id: str, cursor: int, *, timeout_s: float = EVENT_STREAM_TIMEOUT_S
    ) -> list[SseFrame]:
        """Read a request's stored events over the signed event stream.

        Raises:
            DeploymentError: On a non-2xx answer, an unparseable frame, a
                ``dropped`` frame, or the timeout elapsing without a terminal
                frame.
        """
        connection, response = self._open_event_response(
            request_id, cursor, timeout_s=timeout_s
        )
        try:
            return _read_sse_frames(response, request_id=request_id)
        except OSError as exc:
            raise DeploymentError(
                f"the event stream for request {request_id} produced no terminal frame "
                f"within {timeout_s:g}s (the request may still be running): {exc}"
            ) from exc
        finally:
            connection.close()

    def open_event_stream(
        self, request_id: str, cursor: int, *, timeout_s: float = EVENT_STREAM_TIMEOUT_S
    ) -> LiveEventStream:
        """Open a live, non-blocking read of a request's events from `cursor`.

        The returned stream is already connected: the server has replayed
        nothing yet, but the response headers are in hand. Call
        `LiveEventStream.start` to begin reading and `poll` to consume. Passing
        the highest cursor already seen is what makes a reconnect pick up
        exactly where the previous stream stopped, with neither a gap nor a
        duplicate.

        Raises:
            DeploymentError: On a non-2xx answer or a transport failure.
        """
        connection, response = self._open_event_response(
            request_id, cursor, timeout_s=timeout_s
        )
        return LiveEventStream(
            request_id=request_id,
            cursor=cursor,
            connection=connection,
            response=response,
        )


def _read_sse_frames(response: Any, *, request_id: str) -> list[SseFrame]:
    """Parse an SSE response body into frames, stopping at ``done``."""
    return list(_iter_sse_frames(response, request_id=request_id))


def _iter_sse_frames(response: Any, *, request_id: str) -> Iterator[SseFrame]:
    """Yield frames from an SSE response body until ``done`` or the stream ends.

    A generator rather than a list builder, so the live consumer can present
    each ``output.delta`` the moment its frame ends instead of after the request
    settles. `_read_sse_frames` is the buffered wrapper.

    Yields:
        Each complete frame, in order, ending with the terminal ``done`` frame
        when the server sent one.

    Raises:
        DeploymentError: If a frame is unparseable, carries no data, or is the
            gateway's ``dropped`` notice.
    """
    cursor: int | None = None
    event: str | None = None
    data_lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith(":"):
            continue
        if not line:
            if event is None and not data_lines:
                continue
            payload = _frame_data(data_lines, request_id=request_id)
            name = event or "message"
            if name == "dropped":
                reason = payload.get("reason", "no reason given")
                raise DeploymentError(
                    f"the gateway dropped the event stream for request {request_id}: {reason}"
                )
            yield SseFrame(cursor=cursor, event=name, payload=payload)
            if name == "done":
                return
            cursor, event, data_lines = None, None, []
            continue
        field, _, value = line.partition(":")
        if field == "id":
            cursor = int(value.strip()) if value.strip().isdigit() else None
        elif field == "event":
            event = value.strip()
        elif field == "data":
            data_lines.append(value.lstrip(" "))


def _frame_data(data_lines: list[str], *, request_id: str) -> dict[str, Any]:
    """Parse one frame's data lines as strict JSON.

    Raises:
        DeploymentError: If the frame carries no data or the data is not a
            JSON object.
    """
    if not data_lines:
        raise DeploymentError(
            f"the event stream for request {request_id} carried a frame with no data"
        )
    try:
        payload = loads_strict("\n".join(data_lines))
    except PotluckError as exc:
        raise DeploymentError(
            f"the event stream for request {request_id} carried an unparseable frame"
        ) from exc
    if not isinstance(payload, dict):
        raise DeploymentError(
            f"the event stream for request {request_id} carried a non-object frame"
        )
    return cast("dict[str, Any]", payload)


def poll_request_until_terminal(
    consumer: dict[str, str], request_id: str, *, timeout_s: float = REQUEST_WAIT_TIMEOUT_S
) -> dict[str, Any]:
    """Poll a request's status document until it settles or turns terminal.

    Used by ``potluck request --wait``. The first answer that carries a
    settlement or a terminal status wins; the loop then stops, so a settled
    request never pays an extra poll.

    Raises:
        DeploymentError: If the deadline passes without a terminal status; the
            message names the last observed status.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        document = consumer_get(consumer, f"/v1/requests/{request_id}")
        status = document.get("status")
        if document.get("settlement") is not None or status in TERMINAL_REQUEST_STATES:
            return document
        if time.monotonic() >= deadline:
            raise DeploymentError(
                f"request {request_id} did not reach a terminal state within "
                f"{timeout_s:g}s; last status {status!r}"
            )
        time.sleep(0.2)
