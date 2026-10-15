"""The loopback endpoint, driven over a real socket against a scripted server.

Nothing here stubs the transport. The endpoint is the real
`aipotluck.localapi.LocalEndpointServer` on a loopback port, the tool side is a
plain `http.client` request of the kind an OpenAI or Anthropic client makes, and
the server side is a real `http.server.ThreadingHTTPServer` speaking the
platform's own consumer surface: canonical-JSON bodies, an SSE event stream, a
status document. The socket layer is exercised rather than mocked because the
socket layer *is* what is under test — which status a refusal carries, whether a
stream opens before an answer exists, whether an interrupted endpoint cancels the
work it had in flight, and whether a credential ever reaches a response.

The server is scripted, not simulated: it stores the events a test tells it to
and answers the reads a consumer makes. It does not verify the authorization
signature — the registry that could verify it lives server-side, and the
`ai-potluck-server` suite does that beside it — but it records every body it
admitted, so a test can assert what this machine actually signed and submitted.
"""

from __future__ import annotations

import http.client
import http.server
import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from aipotluck.canonical import base64_encode, canonicalize, loads_strict
from aipotluck.cli import main
from aipotluck.identity import generate_key_pair
from aipotluck.localapi import (
    LOCAL_API_KEY_PLACEHOLDER,
    LOCAL_ENDPOINT_SOCKET_TIMEOUT_S,
    EndpointSettings,
    LocalEndpointServer,
    LocalIdentity,
    NonLoopbackBindError,
    _handler_class,
    _InFlight,
    require_loopback,
)
from aipotluck.statemachine import TERMINAL_REQUEST_STATES
from aipotluck.wire import compute_request_digest, new_session_id, user_message

MODEL = "coding-pool"
SUBJECT = "consumer-local-1"
RECEIPT = "R-77"

#: A question whose answer arrives in two increments, so a stream has something
#: to be incremental about.
DELTAS = ("The receipt", " settles it.")
ANSWER = "The receipt settles it."


# --------------------------------------------------------------------------- #
# The server side
# --------------------------------------------------------------------------- #


@dataclass
class Script:
    """How the scripted server answers the next request it admits.

    Attributes:
        deltas: The increments the request produces, in order.
        status: The terminal status, applied at admission unless `hold`.
        billed: The token total the winning attempt's row reports.
        split: The node's own input/output measurement, as a ``usage.final`` event.
        receipt_id: The receipt the settlement was written under, when there is one.
        billing_note: The server's own reason for a request that did not succeed.
        hold: Keep the request unfinished until the test says otherwise, which is
            how a stream is made to deliver increments before the answer exists
            and how a wait window is made to close on unfinished work.
    """

    deltas: tuple[str, ...] = ()
    status: str = "SUCCEEDED"
    billed: int | None = None
    split: tuple[int, int] | None = None
    receipt_id: str | None = None
    billing_note: str | None = None
    hold: bool = False


@dataclass
class FakeRequest:
    """One request the server admitted, with the events it has stored."""

    request_id: str
    body: dict[str, Any]
    status: str = "RUNNING"
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    billed: int | None = None
    receipt_id: str | None = None
    billing_note: str | None = None

    def status_document(self) -> dict[str, Any]:
        """Return the status document a consumer reads back."""
        attempt: dict[str, Any] = {"attempt_id": "A1"}
        if self.billed is not None:
            attempt["usage_tokens"] = self.billed
        document: dict[str, Any] = {
            "request_id": self.request_id,
            "status": self.status,
            "selected_attempt_id": "A1",
            "attempts": [attempt],
            "settlement": {"receipt_id": self.receipt_id} if self.receipt_id else None,
        }
        if self.billing_note:
            document["billing_note"] = self.billing_note
        return document


def _answer_json(
    handler: http.server.BaseHTTPRequestHandler, status: int, document: Mapping[str, Any]
) -> None:
    """Answer one server request with a canonical JSON document.

    Canonical rather than `json.dumps`: the consumer reads these bodies with
    `canonical.loads_strict`, so a float or a repeated key here would be a bug in
    the fixture that the client would report as a broken server.
    """
    body = canonicalize(dict(document))
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class FakeServer:
    """A loopback server speaking the platform's own consumer HTTP surface."""

    def __init__(self, *, models: Sequence[str] = ()) -> None:
        """Bind the listener and record nothing yet.

        Args:
            models: The models the connected, sharing nodes advertise.
        """
        self._lock = threading.RLock()
        self._models = tuple(models)
        self._requests: dict[str, FakeRequest] = {}
        self.admitted: list[dict[str, Any]] = []
        self.cancels: list[str] = []
        self.errors: list[BaseException] = []
        self.script = Script()
        self.refusal: tuple[int, str, str] | None = None
        self._httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), self._handler_class()
        )
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    # -- what a test controls ------------------------------------------------

    @property
    def url(self) -> str:
        """Return the ``http://`` base URL of this server."""
        return f"http://127.0.0.1:{int(self._httpd.server_address[1])}"

    def start(self) -> None:
        """Begin serving."""
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and re-raise anything a handler hit."""
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5.0)
        if self.errors:
            raise AssertionError(f"the scripted server hit {self.errors[0]!r}")

    def answer_with(self, **script: Any) -> None:
        """Set how the next admitted request behaves."""
        self.script = Script(**script)

    def refuse_with(self, status: int, code: str, detail: str) -> None:
        """Make admission fail the way a server refuses a request."""
        self.refusal = (status, code, detail)

    @property
    def last(self) -> FakeRequest:
        """Return the most recently admitted request."""
        with self._lock:
            return self._requests[self.admitted[-1]["authorization"]["payload"]["request_id"]]

    def emit(self, text: str) -> None:
        """Store one more increment on the most recently admitted request."""
        with self._lock:
            self.last.events.append(
                ("output.delta", {"text": text, "attempt_id": "A1"})
            )

    def finish(
        self,
        *,
        status: str = "SUCCEEDED",
        billed: int | None = None,
        split: tuple[int, int] | None = None,
        receipt_id: str | None = None,
        billing_note: str | None = None,
    ) -> None:
        """End the most recently admitted request, storing its final measurement."""
        with self._lock:
            request = self.last
            if split is not None:
                request.events.append(
                    (
                        "usage.final",
                        {"usage": {"input_total": split[0], "output_total": split[1]}},
                    )
                )
            request.status = status
            request.billed = billed
            request.receipt_id = receipt_id
            request.billing_note = billing_note

    # -- the routes ----------------------------------------------------------

    def _handler_class(self) -> type[http.server.BaseHTTPRequestHandler]:
        """Return the handler bound to this server."""
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            """One server route per method, recording what it was sent."""

            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                """Silence the access log."""
                del format, args

            def do_POST(self) -> None:
                """Admit a request, or cancel one."""
                try:
                    raw = self._body()
                    parts = _route_parts(self.path)
                    if parts == ["v1", "requests"]:
                        server._admit(raw, self)
                    elif _is_request_action(parts, "cancel"):
                        server._cancel(parts[2], self)
                    else:
                        _answer_json(self, 404, {"error": "not_found", "detail": self.path})
                except BaseException as exc:  # a fixture bug must not hang a test
                    server.errors.append(exc)
                    self.close_connection = True

            def do_GET(self) -> None:
                """Serve the node list, one status document, or one event stream."""
                try:
                    parts = _route_parts(self.path)
                    if parts == ["v1", "nodes"]:
                        server._nodes(self)
                    elif len(parts) == 3 and parts[:2] == ["v1", "requests"]:
                        server._status(parts[2], self)
                    elif _is_request_action(parts, "events"):
                        server._events(parts[2], self)
                    else:
                        _answer_json(self, 404, {"error": "not_found", "detail": self.path})
                except BaseException as exc:
                    server.errors.append(exc)
                    self.close_connection = True

            def _body(self) -> bytes:
                """Read this request's body."""
                length = int(self.headers.get("content-length", "0") or "0")
                return self.rfile.read(length)

        return Handler

    def _admit(self, raw: bytes, handler: http.server.BaseHTTPRequestHandler) -> None:
        """Admit one submitted request, applying the current script."""
        if self.refusal is not None:
            status, code, detail = self.refusal
            _answer_json(handler, status, {"error": code, "detail": detail})
            return
        body = loads_strict(raw)
        if not isinstance(body, dict):
            raise AssertionError("the endpoint submitted a non-object body")
        authorization = body["authorization"]
        request_id = str(authorization["payload"]["request_id"])
        script = self.script
        with self._lock:
            self.admitted.append(body)
            request = FakeRequest(request_id=request_id, body=body)
            self._requests[request_id] = request
            for text in script.deltas:
                request.events.append(
                    ("output.delta", {"text": text, "attempt_id": "A1"})
                )
            if not script.hold:
                request.status = script.status
                request.billed = script.billed
                request.receipt_id = script.receipt_id
                request.billing_note = script.billing_note
                if script.split is not None:
                    request.events.append(
                        (
                            "usage.final",
                            {
                                "usage": {
                                    "input_total": script.split[0],
                                    "output_total": script.split[1],
                                }
                            },
                        )
                    )
        _answer_json(handler, 200, {"request_id": request_id})

    def _cancel(self, request_id: str, handler: http.server.BaseHTTPRequestHandler) -> None:
        """Record one cancellation and end the request as cancelled."""
        with self._lock:
            self.cancels.append(request_id)
            request = self._requests.get(request_id)
            if request is not None:
                request.status = "CANCELLED"
        _answer_json(handler, 200, {"request_id": request_id, "status": "CANCELLED"})

    def _nodes(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        """Answer with one connected, sharing node per advertised model."""
        _answer_json(
            handler,
            200,
            {
                "nodes": [
                    {
                        "node_id": f"node-{index}",
                        "model": model,
                        "share_enabled": True,
                        "last_seen_at": "2026-01-02T00:00:00+00:00",
                    }
                    for index, model in enumerate(self._models)
                ]
            },
        )

    def _status(self, request_id: str, handler: http.server.BaseHTTPRequestHandler) -> None:
        """Answer with one request's status document."""
        with self._lock:
            request = self._requests.get(request_id)
            document = request.status_document() if request else None
        if document is None:
            _answer_json(handler, 404, {"error": "not_found", "detail": request_id})
            return
        _answer_json(handler, 200, document)

    def _events(self, request_id: str, handler: http.server.BaseHTTPRequestHandler) -> None:
        """Replay the stored events after the caller's cursor, as SSE.

        A request that is still unfinished gets what exists and then a closed
        stream, which is what makes the consumer reconnect from its cursor: the
        same behaviour the real server has for a live request, and the reason a
        reconnect duplicates neither text nor charge.
        """
        cursor = _cursor_of(handler.path)
        with self._lock:
            request = self._requests.get(request_id)
            events = list(request.events[cursor:]) if request else []
            status = request.status if request else "FAILED"
            terminal = status in TERMINAL_REQUEST_STATES
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("connection", "close")
        handler.end_headers()
        handler.close_connection = True
        for index, (name, payload) in enumerate(events, start=cursor + 1):
            handler.wfile.write(
                f"id: {index}\nevent: {name}\ndata: ".encode()
                + canonicalize(payload)
                + b"\n\n"
            )
        if terminal:
            handler.wfile.write(
                b"event: done\ndata: "
                + canonicalize({"request_id": request_id, "status": status})
                + b"\n\n"
            )
        handler.wfile.flush()


def _route_parts(path: str) -> list[str]:
    """Return one request target's path segments, without its query."""
    return [part for part in path.split("?")[0].split("/") if part]


def _is_request_action(parts: Sequence[str], action: str) -> bool:
    """Return whether a route is one action on one request."""
    return (
        len(parts) == 4
        and parts[0] == "v1"
        and parts[1] == "requests"
        and parts[3] == action
    )


def _cursor_of(path: str) -> int:
    """Return the ``cursor`` query parameter of an event-stream request."""
    _, _, query = path.partition("?")
    for part in query.split("&"):
        name, _, value = part.partition("=")
        if name == "cursor" and value.isdigit():
            return int(value)
    return 0


# --------------------------------------------------------------------------- #
# The tool side, and the fixtures that put the two together
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Identity:
    """One machine's saved consumer identity."""

    directory: Path
    config_document: dict[str, Any]
    key_document: dict[str, Any]

    @property
    def private_key(self) -> str:
        """Return the signing key that must never leave this machine."""
        return str(self.key_document["private_key_b64"])


def _write_identity(directory: Path, server_url: str) -> Identity:
    """Write the two files a ``join`` leaves behind and return them."""
    public_key_b64, private_key_b64 = generate_key_pair()
    config_document: dict[str, Any] = {
        "server_url": server_url,
        "key_id": "consumer-key-1",
        "subject_id": SUBJECT,
        "tenant_id": "tenant-1",
        "role": "consumer",
    }
    key_document = {
        "public_key_b64": public_key_b64,
        "private_key_b64": private_key_b64,
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_bytes(canonicalize(config_document))
    (directory / "key.json").write_bytes(canonicalize(key_document))
    return Identity(
        directory=directory,
        config_document=config_document,
        key_document=key_document,
    )


@pytest.fixture
def server() -> Iterator[FakeServer]:
    """Return a running scripted server that advertises one model."""
    instance = FakeServer(models=(MODEL,))
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture
def identity(server: FakeServer, tmp_path: Path) -> Identity:
    """Return a joined identity whose server is the scripted one."""
    return _write_identity(tmp_path / "aipotluck", server.url)


@pytest.fixture
def endpoint(identity: Identity) -> Iterator[Callable[..., LocalEndpointServer]]:
    """Return a factory that starts one endpoint on a free loopback port."""
    servers: list[LocalEndpointServer] = []

    def start(**settings: Any) -> LocalEndpointServer:
        endpoint_server = LocalEndpointServer(
            identity=LocalIdentity.load(identity.directory),
            settings=EndpointSettings(port=0, **settings),
        )
        endpoint_server.start()
        servers.append(endpoint_server)
        return endpoint_server

    yield start
    for endpoint_server in servers:
        endpoint_server.stop(grace_s=1.0)


def _connect(endpoint_server: LocalEndpointServer) -> http.client.HTTPConnection:
    """Open one connection to the endpoint."""
    return http.client.HTTPConnection("127.0.0.1", endpoint_server.port, timeout=30.0)


def _send(
    connection: http.client.HTTPConnection,
    method: str,
    path: str,
    document: Any = None,
    *,
    raw_body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> http.client.HTTPResponse:
    """Send one tool request and return the open response."""
    body = raw_body if raw_body is not None else (
        json.dumps(document).encode() if document is not None else None
    )
    sent = {
        "content-type": "application/json",
        "authorization": f"Bearer {LOCAL_API_KEY_PLACEHOLDER}",
        **(dict(headers) if headers else {}),
    }
    connection.request(method, path, body=body, headers=sent)
    return connection.getresponse()


def _call(
    endpoint_server: LocalEndpointServer,
    method: str,
    path: str,
    document: Any = None,
    *,
    raw_body: bytes | None = None,
) -> tuple[int, http.client.HTTPMessage, Any, bytes]:
    """Make one buffered tool request, returning status, headers, body and bytes."""
    connection = _connect(endpoint_server)
    try:
        response = _send(connection, method, path, document, raw_body=raw_body)
        raw = response.read()
        status, headers = response.status, response.headers
    finally:
        connection.close()
    parsed = json.loads(raw) if raw else None
    return status, headers, parsed, raw


def _chat(**overrides: Any) -> dict[str, Any]:
    """Return one OpenAI chat-completions body asking the shared question."""
    document: dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "what settles a request?"}],
    }
    document.update(overrides)
    return document


def _read_frames(
    response: http.client.HTTPResponse, limit: int | None = None
) -> list[tuple[str, str]]:
    """Read SSE frames off an open response as ``(event name, data)`` pairs.

    Stops after `limit` frames when given, which is what lets a test assert that
    an increment was on the wire *before* the answer existed rather than after.
    """
    frames: list[tuple[str, str]] = []
    event = ""
    data: list[str] = []
    while True:
        line = response.readline()
        if not line:
            break
        text = line.decode("utf-8").rstrip("\r\n")
        if text.startswith("event:"):
            event = text.split(":", 1)[1].strip()
        elif text.startswith("data:"):
            data.append(text.split(":", 1)[1].strip())
        elif not text and data:
            frames.append((event, "\n".join(data)))
            event, data = "", []
            if limit is not None and len(frames) >= limit:
                break
    return frames


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 15.0) -> None:
    """Wait for a condition another thread is making true."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("the condition was never met")


# --------------------------------------------------------------------------- #
# The one security invariant
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "::1", "8.8.8.8", "10.0.0.4", "localhost", "example.com", ""],
)
def test_an_address_that_is_not_ipv4_loopback_is_refused(host: str) -> None:
    """Every interface, every public address, every name, and the IPv6 loopback.

    A hostname is refused even when it would resolve to loopback: resolving it is
    done through host files this process does not own, so "resolve, then hope" is
    not an invariant. ``::1`` is refused because this is an IPv4 listener and a
    second address family is a second way to be wrong.
    """
    with pytest.raises(NonLoopbackBindError) as raised:
        require_loopback(host)

    assert "loopback" in str(raised.value)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.3.4.5"])
def test_any_ipv4_loopback_address_is_accepted(host: str) -> None:
    """The whole 127.0.0.0/8 is loopback, so the whole of it may be bound."""
    assert require_loopback(host) == host


def test_the_listener_refuses_to_come_up_on_a_published_address(
    identity: Identity,
) -> None:
    """The invariant is enforced at construction, not only by the flag parser.

    A programmatic caller gets the same refusal as ``--host 0.0.0.0``, and
    nothing is left listening afterwards.
    """
    with pytest.raises(NonLoopbackBindError):
        LocalEndpointServer(
            identity=LocalIdentity.load(identity.directory),
            settings=EndpointSettings(host="0.0.0.0", port=0),
        )


def test_the_command_line_refuses_a_non_loopback_host(
    identity: Identity, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal to start is never a silent one."""
    code = main(
        [
            "serve-openai",
            "--host",
            "0.0.0.0",
            "--config-dir",
            str(identity.directory),
        ]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "loopback" in captured.err


def test_a_directory_with_no_identity_in_it_refuses_to_serve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An endpoint that cannot sign is not an endpoint, and says how to become one."""
    code = main(["serve-openai", "--config-dir", str(tmp_path / "empty"), "--port", "0"])
    captured = capsys.readouterr()

    assert code == 1
    assert "join" in captured.err


# --------------------------------------------------------------------------- #
# OpenAI's shape
# --------------------------------------------------------------------------- #


def test_a_buffered_completion_is_the_pools_answer_with_the_counts_it_reported(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """One ``chat.completion``, and the platform's own handles in the headers.

    The request id and the receipt id are what let an operator take an answer
    they were just served and verify the charge behind it, so they travel in the
    body's reach even though a tool ignores headers it does not know.
    """
    server.answer_with(deltas=DELTAS, billed=1234, split=(1100, 134), receipt_id=RECEIPT)
    endpoint_server = endpoint()

    status, headers, document, _raw = _call(
        endpoint_server, "POST", "/v1/chat/completions", _chat()
    )

    assert status == 200
    assert document["object"] == "chat.completion"
    assert document["model"] == MODEL
    assert document["id"].startswith("chatcmpl-")
    choice = document["choices"][0]
    assert choice["message"] == {
        "role": "assistant",
        "content": ANSWER,
        "refusal": None,
    }
    assert choice["finish_reason"] == "stop"
    assert document["usage"] == {
        "prompt_tokens": 1100,
        "completion_tokens": 134,
        "total_tokens": 1234,
    }
    request_id = headers["x-potluck-request-id"]
    assert request_id and headers["x-potluck-receipt-id"] == RECEIPT


def test_the_conversation_a_tool_sent_is_what_this_machine_signed_for(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """Every turn reaches the server verbatim, under this machine's own identity.

    This is the whole claim of the endpoint: a tool needs a base URL and no
    potluck-specific code, and what the server receives is the conversation the
    tool sent plus a signature the tool never sees and cannot make.
    """
    server.answer_with(deltas=DELTAS)
    endpoint_server = endpoint()
    conversation = [
        {"role": "system", "content": "answer in one word"},
        {"role": "user", "content": "what settles a request?"},
        {"role": "assistant", "content": "a receipt"},
        {"role": "user", "content": "and what backs the receipt?"},
    ]

    _call(endpoint_server, "POST", "/v1/chat/completions", _chat(messages=conversation))

    assert len(server.admitted) == 1
    body = server.admitted[0]
    assert body["messages"] == conversation
    assert body["model"] == MODEL
    assert body["delivery_mode"] == "buffered_complete"
    payload = body["authorization"]["payload"]
    assert payload["consumer_id"] == SUBJECT
    assert payload["payer_account_id"] == SUBJECT
    assert body["authorization"]["signer_id"] == SUBJECT


def test_a_forwarded_parameter_reaches_the_server_inside_the_signed_body(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """What the tool asked for is what the node runs, and what the digest covers."""
    server.answer_with(deltas=DELTAS)
    endpoint_server = endpoint()

    _call(endpoint_server, "POST", "/v1/chat/completions", _chat(max_tokens=64, temperature=1.0))

    assert server.admitted[0]["parameters"] == {"max_tokens": 64, "temperature": 1}


def test_every_request_one_endpoint_signs_belongs_to_its_own_single_session(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """One endpoint run is one session, and both shapes report the same one.

    The session is what lets the server aggregate an agent loop's turns and
    prefer the node that served its last one, so the property that matters is
    that a second call does not start a second session. Asserted over both tool
    shapes because they are two doors into the same signing path.
    """
    server.answer_with(deltas=DELTAS)
    endpoint_server = endpoint()

    _call(endpoint_server, "POST", "/v1/chat/completions", _chat())
    _call(
        endpoint_server,
        "POST",
        "/v1/messages",
        {
            "model": MODEL,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": "what settles a request?"}],
        },
    )

    sessions = [body.get("session_id") for body in server.admitted]
    assert sessions == [endpoint_server.session_id, endpoint_server.session_id]


def test_two_endpoint_runs_are_two_sessions(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """A restart is a new session, not a continuation of the one that stopped."""
    first = endpoint()
    second = endpoint()

    assert first.session_id != second.session_id


def test_a_session_is_a_label_the_signature_does_not_cover(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """The digest is the same with a session as without one.

    A session says which run a turn belongs to; it is not work a node executes.
    Signing over it would mean a caller has to re-sign to move a turn between its
    own sessions, and would buy nothing: the label can only attribute a request
    to a session of the subject the credential already authenticated.
    """
    server.answer_with(deltas=DELTAS)
    endpoint_server = endpoint()

    _call(endpoint_server, "POST", "/v1/chat/completions", _chat(max_tokens=64))

    body = server.admitted[0]
    payload = body["authorization"]["payload"]
    assert payload["request_digest"] == compute_request_digest(
        messages=(user_message("what settles a request?"),),
        model=MODEL,
        delivery_mode="buffered_complete",
        parameters={"max_tokens": 64},
    )


def test_a_stream_carries_the_answer_in_pieces_and_ends_with_done(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """The documented OpenAI chunk sequence, including the terminal usage frame.

    ``stream_options.include_usage`` is honoured because a client that asked for
    totals reads them from that frame and from nowhere else.
    """
    server.answer_with(deltas=DELTAS, billed=1234, split=(1100, 134))
    endpoint_server = endpoint()
    connection = _connect(endpoint_server)
    try:
        response = _send(
            connection,
            "POST",
            "/v1/chat/completions",
            _chat(stream=True, stream_options={"include_usage": True}),
        )
        assert response.status == 200
        assert response.headers["content-type"] == "text/event-stream"
        assert response.headers["x-potluck-request-id"]
        frames = _read_frames(response)
    finally:
        connection.close()

    documents = [json.loads(data) for _event, data in frames if data != "[DONE]"]
    assert frames[-1][1] == "[DONE]", "a client's read loop waits for the terminator"
    assert documents[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert [
        document["choices"][0]["delta"].get("content")
        for document in documents[1:3]
    ] == list(DELTAS)
    assert documents[3]["choices"][0]["finish_reason"] == "stop"
    assert documents[3]["choices"][0]["delta"] == {}
    assert documents[4]["choices"] == []
    assert documents[4]["usage"]["total_tokens"] == 1234
    assert {document["id"] for document in documents} == {documents[0]["id"]}


def test_a_stream_releases_increments_before_the_answer_exists(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """Buffering a stream and calling it streaming is the one lie not to tell.

    The first increment is read off the socket while the server still reports the
    request as unfinished, and the rest only after the test lets it finish.
    """
    server.answer_with(deltas=(DELTAS[0],), hold=True)
    endpoint_server = endpoint()
    connection = _connect(endpoint_server)
    try:
        response = _send(
            connection,
            "POST",
            "/v1/chat/completions",
            _chat(stream=True, stream_options={"include_usage": True}),
        )
        early = _read_frames(response, limit=2)
        assert [json.loads(data)["choices"][0]["delta"] for _event, data in early] == [
            {"role": "assistant"},
            {"content": DELTAS[0]},
        ]
        assert server.last.status == "RUNNING", "nothing had finished yet"

        server.emit(DELTAS[1])
        server.finish(billed=99, split=(90, 9))
        rest = _read_frames(response)
    finally:
        connection.close()

    data = [json.loads(frame[1]) for frame in rest if frame[1] != "[DONE]"]
    assert data[0]["choices"][0]["delta"] == {"content": DELTAS[1]}
    assert data[-1]["choices"] == [], "the totals frame carries no increment"
    assert data[-1]["usage"] == {
        "prompt_tokens": 90,
        "completion_tokens": 9,
        "total_tokens": 99,
    }
    assert rest[-1][1] == "[DONE]"


def test_a_stream_without_a_usage_request_carries_no_usage_frame(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """A totals frame nobody asked for is a frame a client has to explain away."""
    server.answer_with(deltas=DELTAS, billed=1234, split=(1100, 134))
    endpoint_server = endpoint()
    connection = _connect(endpoint_server)
    try:
        response = _send(connection, "POST", "/v1/chat/completions", _chat(stream=True))
        frames = _read_frames(response)
    finally:
        connection.close()

    documents = [json.loads(data) for _event, data in frames if data != "[DONE]"]
    assert all("usage" not in document for document in documents)


def test_the_model_list_is_what_the_connected_nodes_advertise(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """A tool picks a model from this list, so it names the pool and not a catalogue."""
    endpoint_server = endpoint()

    status, _headers, document, _raw = _call(endpoint_server, "GET", "/v1/models")

    assert status == 200
    assert document["object"] == "list"
    assert [model["id"] for model in document["data"]] == [MODEL]
    assert document["data"][0]["owned_by"] == "ai-potluck"


# --------------------------------------------------------------------------- #
# Anthropic's shape: the surface Claude Code speaks
# --------------------------------------------------------------------------- #


def test_the_anthropic_surface_answers_in_its_own_shape(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """One buffered ``message``, with the platform's own counts and no invented ones.

    ``stop_reason`` is ``end_turn`` because the platform carries no upstream stop
    reason back; the one thing a renderer can say is that the answer is complete.
    """
    server.answer_with(deltas=DELTAS, billed=1234, split=(1100, 134), receipt_id=RECEIPT)
    endpoint_server = endpoint()

    status, headers, document, _raw = _call(
        endpoint_server,
        "POST",
        "/v1/messages",
        {
            "model": MODEL,
            "max_tokens": 512,
            "system": "answer in one word",
            "messages": [{"role": "user", "content": "what settles a request?"}],
        },
    )

    assert status == 200
    assert document["type"] == "message"
    assert document["role"] == "assistant"
    assert document["model"] == MODEL
    assert document["id"].startswith("msg_")
    assert document["content"] == [{"type": "text", "text": ANSWER}]
    assert document["stop_reason"] == "end_turn"
    assert document["usage"] == {"input_tokens": 1100, "output_tokens": 134}
    assert headers["x-potluck-receipt-id"] == RECEIPT
    # The reshaping Anthropic's shape needs: its top-level `system` becomes the
    # conversation's first turn, and its required `max_tokens` is forwarded.
    body = server.admitted[0]
    assert body["messages"][0] == {"role": "system", "content": "answer in one word"}
    assert body["messages"][1]["role"] == "user"
    assert body["parameters"] == {"max_tokens": 512}


def test_the_anthropic_stream_is_the_documented_event_sequence(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """A client that accumulates a streamed message opens its block from the start event.

    Every frame names its event twice — in the ``event:`` line and in the
    document's own ``type`` — and the two agree, because a client reads one of
    them and a debugger reads the other.
    """
    server.answer_with(deltas=DELTAS, billed=1234, split=(1100, 134))
    endpoint_server = endpoint()
    connection = _connect(endpoint_server)
    try:
        response = _send(
            connection,
            "POST",
            "/v1/messages",
            {"model": MODEL, "max_tokens": 512, "stream": True,
             "messages": [{"role": "user", "content": "what settles a request?"}]},
        )
        assert response.status == 200
        assert response.headers["content-type"] == "text/event-stream"
        frames = _read_frames(response)
    finally:
        connection.close()

    events = [event for event, _data in frames]
    assert events == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    documents = [json.loads(data) for _event, data in frames]
    assert all(
        document["type"] == event
        for event, document in zip(events, documents, strict=True)
    )
    assert documents[0]["message"]["id"].startswith("msg_")
    assert documents[0]["message"]["content"] == []
    assert documents[1]["content_block"] == {"type": "text", "text": ""}
    assert [document["delta"]["text"] for document in documents[2:4]] == list(DELTAS)
    assert documents[5]["delta"] == {"stop_reason": "end_turn", "stop_sequence": None}
    assert documents[5]["usage"] == {"output_tokens": 134}


def test_an_anthropic_request_without_max_tokens_gets_a_ceiling_not_a_refusal(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """The API requires the field; a tool that omits it still gets a bounded request."""
    server.answer_with(deltas=DELTAS)
    endpoint_server = endpoint()

    status, _headers, _document, _raw = _call(
        endpoint_server,
        "POST",
        "/v1/messages",
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert status == 200
    assert server.admitted[0]["parameters"]["max_tokens"] > 0


# --------------------------------------------------------------------------- #
# Failures, told apart
# --------------------------------------------------------------------------- #


def test_a_request_the_server_ended_in_failure_is_an_error_naming_the_request(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """The server's own reason, the platform's request id, and the way to check the cost.

    A FAILED request is settled provisionally, so rendering it as a short answer
    would put "this was free" in front of a caller whose upstream really did burn
    tokens. The ``usage`` block is absent rather than zero for the same reason.
    """
    server.answer_with(
        deltas=(),
        status="FAILED",
        receipt_id=RECEIPT,
        billing_note="no node left to take the work: local budget would be exceeded",
    )
    endpoint_server = endpoint()

    status, headers, document, _raw = _call(
        endpoint_server, "POST", "/v1/chat/completions", _chat()
    )

    assert status == 502
    error = document["error"]
    assert error["code"] == "upstream_failed"
    assert error["type"] == "server_error"
    assert "no node left to take the work" in error["message"], "the server's own words"
    assert error["request_id"] == headers["x-potluck-request-id"]
    assert "verify receipt" in error["message"]
    assert RECEIPT in error["message"]
    assert "usage to be confirmed" in error["message"]
    assert "usage" not in document


def test_a_wait_window_that_closed_on_unfinished_work_is_a_deadline(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """``504``, and a message that says the work may still be running and still be billed.

    The endpoint stopped waiting; the platform did not stop working. Reporting
    that as an upstream failure would claim an ending nobody observed.
    """
    server.answer_with(hold=True)
    endpoint_server = endpoint(timeout_s=0.6)

    status, headers, document, _raw = _call(
        endpoint_server, "POST", "/v1/chat/completions", _chat()
    )

    assert status == 504
    assert document["error"]["code"] == "deadline_passed"
    assert document["error"]["request_id"] == headers["x-potluck-request-id"]
    message = document["error"]["message"]
    assert "did not reach a terminal state" in message
    assert "may still be running" in message
    assert "usage to be confirmed" in message


def test_a_streaming_wait_that_closes_unfinished_is_a_deadline_too(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """The delivery mode changes the frames, not what the outcome means."""
    server.answer_with(hold=True)
    endpoint_server = endpoint(timeout_s=0.6)

    status, _headers, document, _raw = _call(
        endpoint_server, "POST", "/v1/chat/completions", _chat(stream=True)
    )

    assert status == 504, "the stream never opened, so its real status is still available"
    assert document["error"]["code"] == "deadline_passed"


def test_a_server_refusal_is_mapped_into_the_callers_own_vocabulary(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """An exhausted balance must not read as an outage, in either shape."""
    server.refuse_with(402, "insufficient_funds", "consumer-X has no credit left")
    endpoint_server = endpoint()

    openai_status, _headers, openai_document, _raw = _call(
        endpoint_server, "POST", "/v1/chat/completions", _chat()
    )
    anthropic_status, _headers, anthropic_document, _raw = _call(
        endpoint_server,
        "POST",
        "/v1/messages",
        {"model": MODEL, "max_tokens": 16,
         "messages": [{"role": "user", "content": "hi"}]},
    )

    assert openai_status == 429
    assert openai_document["error"]["code"] == "insufficient_quota"
    assert openai_document["error"]["type"] == "insufficient_quota"
    assert "no credit left" in openai_document["error"]["message"]
    assert anthropic_status == 429
    assert anthropic_document["type"] == "error"
    assert anthropic_document["error"]["type"] == "rate_limit_error"


def test_a_failure_after_a_stream_opened_travels_inside_the_stream(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """Once the ``200`` and the SSE head are out there is no status left to send.

    The failure is an error frame, and the stream still ends with its terminator:
    a client whose read loop waits for one would otherwise sit on a closed socket
    wondering whether more was coming.
    """
    server.answer_with(deltas=(DELTAS[0],), hold=True)
    endpoint_server = endpoint(timeout_s=60.0)
    connection = _connect(endpoint_server)
    try:
        response = _send(connection, "POST", "/v1/chat/completions", _chat(stream=True))
        _read_frames(response, limit=2)
        server.finish(status="FAILED", billing_note="the node stopped answering")
        frames = _read_frames(response)
    finally:
        connection.close()

    assert frames[-1][1] == "[DONE]"
    error = json.loads(frames[-2][1])["error"]
    assert error["code"] == "upstream_failed"
    assert "the node stopped answering" in error["message"]
    assert error["request_id"]


def test_a_fractional_parameter_is_refused_before_anything_is_signed(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """Every offending parameter at once, in the caller's shape, costing nothing.

    The signed path cannot carry a fraction, and silently dropping one would run
    a request the caller did not ask for. The refusal is raised while the body is
    still being parsed, so no authorization was minted and the server never saw
    the request — which is what makes "this cost nothing" a true statement.
    """
    endpoint_server = endpoint()

    status, _headers, document, _raw = _call(
        endpoint_server, "POST", "/v1/chat/completions", _chat(temperature=0.7, top_p=0.9)
    )

    assert status == 400
    assert document["error"]["code"] == "invalid_request"
    assert document["error"]["type"] == "invalid_request_error"
    message = document["error"]["message"]
    assert "temperature" in message and "top_p" in message
    assert "/openai/v1" in message, "the surface that can carry a fraction is named"
    assert server.admitted == []


def test_a_fractional_parameter_is_refused_in_anthropics_shape_too(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """Claude Code sends ``temperature``; it reads the error shape it parses."""
    endpoint_server = endpoint()

    status, _headers, document, _raw = _call(
        endpoint_server,
        "POST",
        "/v1/messages",
        {"model": MODEL, "max_tokens": 16, "temperature": 0.7,
         "messages": [{"role": "user", "content": "hi"}]},
    )

    assert status == 400
    assert document["type"] == "error"
    assert document["error"]["type"] == "invalid_request_error"
    assert "temperature" in document["error"]["message"]
    assert server.admitted == []


@pytest.mark.parametrize(
    ("method", "path", "shape"),
    [
        ("POST", "/v1/embeddings", "openai"),
        ("POST", "/v1/messages/count_tokens", "anthropic"),
        ("GET", "/v1/messages", "anthropic"),
        ("GET", "/v1/completions", "openai"),
    ],
)
def test_a_route_this_endpoint_does_not_serve_is_refused_not_executed(
    server: FakeServer,
    endpoint: Callable[..., LocalEndpointServer],
    method: str,
    path: str,
    shape: str,
) -> None:
    """Named rather than unrouted, in the shape the caller parses.

    ``POST /v1/messages/count_tokens`` is the case the exact-match routing exists
    for: a Claude Code client sends it to size a context, and a prefix match
    would run it as a completion and spend credit to answer a question about
    counting.
    """
    endpoint_server = endpoint()
    document_body = (
        {"model": MODEL, "max_tokens": 16,
         "messages": [{"role": "user", "content": "hi"}]}
        if method == "POST"
        else None
    )

    status, _headers, document, _raw = _call(
        endpoint_server, method, path, document_body
    )

    assert status == 404
    error = document["error"]
    if shape == "openai":
        assert error["code"] == "not_supported"
        assert error["type"] == "not_supported"
    else:
        assert error["type"] == "not_found_error"
    assert "POST /v1/chat/completions" in error["message"]
    assert "POST /v1/messages" in error["message"]
    assert server.admitted == []


@pytest.mark.parametrize(
    ("raw_body", "expected"),
    [
        (b"{not json", "not valid JSON"),
        (b'{"model": "m", "model": "n"}', "duplicate JSON key"),
        (b"[1,2]", "must be a JSON object"),
        (b'{"messages": [{"role": "user", "content": "hi"}]}', "non-empty string 'model'"),
        (b'{"model": "m"}', "messages must be a JSON array"),
        (b'{"model": "m", "messages": []}', "at least one message"),
        (b'{"model": "m", "messages": [{"role": "user"}]}', "carries no content field"),
    ],
)
def test_a_body_this_endpoint_cannot_forward_is_a_400_naming_the_problem(
    server: FakeServer,
    endpoint: Callable[..., LocalEndpointServer],
    raw_body: bytes,
    expected: str,
) -> None:
    """A malformed body is answered, not crashed on, and nothing is submitted."""
    endpoint_server = endpoint()

    status, _headers, document, _raw = _call(
        endpoint_server, "POST", "/v1/chat/completions", raw_body=raw_body
    )

    assert status == 400
    assert expected in document["error"]["message"]
    assert server.admitted == []


def test_a_body_with_no_content_length_is_refused_rather_than_half_read(
    endpoint: Callable[..., LocalEndpointServer],
) -> None:
    """A body only half read would be signed as something the caller did not send."""
    endpoint_server = endpoint()
    connection = _connect(endpoint_server)
    try:
        connection.putrequest("POST", "/v1/chat/completions")
        connection.putheader("transfer-encoding", "chunked")
        connection.endheaders()
        response = connection.getresponse()
        raw = response.read()
        status = response.status
    finally:
        connection.close()

    assert status == 411
    assert "Content-Length" in json.loads(raw)["error"]["message"]


def test_a_body_above_the_size_limit_is_refused_by_its_declared_length(
    endpoint: Callable[..., LocalEndpointServer],
) -> None:
    """Refused on the header, so the bytes are never buffered to find that out."""
    from aipotluck.openai_wire import MAX_BODY_BYTES

    endpoint_server = endpoint()
    connection = _connect(endpoint_server)
    try:
        connection.putrequest("POST", "/v1/chat/completions")
        connection.putheader("content-type", "application/json")
        connection.putheader("content-length", str(MAX_BODY_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        raw = response.read()
        status = response.status
    finally:
        connection.close()

    assert status == 413
    assert str(MAX_BODY_BYTES) in json.loads(raw)["error"]["message"]


def test_a_server_that_cannot_be_reached_is_reported_as_an_upstream_failure(
    tmp_path: Path, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """The identity's server is gone; a tool reads one error document, not a traceback."""
    identity = _write_identity(tmp_path / "gone", "http://127.0.0.1:1")
    endpoint_server = LocalEndpointServer(
        identity=LocalIdentity.load(identity.directory),
        settings=EndpointSettings(port=0, timeout_s=2.0),
    )
    endpoint_server.start()
    try:
        status, _headers, document, _raw = _call(
            endpoint_server, "POST", "/v1/chat/completions", _chat()
        )
    finally:
        endpoint_server.stop(grace_s=1.0)

    assert status == 502
    assert document["error"]["code"] == "upstream_failed"
    assert "server unreachable" in document["error"]["message"]


# --------------------------------------------------------------------------- #
# Stopping, and what a stop owes the work in flight
# --------------------------------------------------------------------------- #


def test_stopping_the_endpoint_cancels_the_work_it_had_in_flight(
    server: FakeServer,
    identity: Identity,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A cancel reaches the server, and the caller is answered rather than dropped.

    The cancel is posted rather than merely abandoned locally because the server
    pushes it on to the node running the work: an interrupted endpoint must not
    leave an upstream generating tokens for an answer nobody will read. The caller
    is still connected, so it is owed a document that says the request was
    cancelled and that what it cost is not knowable from here.
    """
    server.answer_with(hold=True)
    endpoint_server = LocalEndpointServer(
        identity=LocalIdentity.load(identity.directory),
        settings=EndpointSettings(port=0, timeout_s=30.0),
    )
    endpoint_server.start()
    outcome: list[tuple[int, Any]] = []

    def call() -> None:
        status, _headers, document, _raw = _call(
            endpoint_server, "POST", "/v1/chat/completions", _chat()
        )
        outcome.append((status, document))

    thread = threading.Thread(target=call, daemon=True)
    try:
        thread.start()
        _wait_until(lambda: bool(server.admitted))
        cancelled = endpoint_server.stop(grace_s=5.0)
        thread.join(timeout=10.0)
    finally:
        if endpoint_server.running:
            endpoint_server.stop(grace_s=1.0)

    assert cancelled == 1, "one request was in flight and was asked to cancel"
    assert server.cancels == [server.last.request_id]
    assert not thread.is_alive()
    status, document = outcome[0]
    assert status == 503
    assert document["error"]["code"] == "service_unavailable"
    assert "this endpoint is stopping" in document["error"]["message"]
    assert "usage to be confirmed" in document["error"]["message"]
    logged = capsys.readouterr().out
    assert "cancel" in logged


def test_a_caller_that_hangs_up_mid_stream_stops_the_work_it_asked_for(
    server: FakeServer,
    identity: Identity,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A disconnect ends the request at the server, not only the writing of it.

    The tool walked away, so there is nobody to answer; what still matters is that
    no upstream keeps generating tokens for an answer nobody will read. The cancel
    reaches the server, which pushes it on to the node running the work, and the
    request's one log line says the caller left rather than pretending it was
    served.
    """
    server.answer_with(deltas=(DELTAS[0],), hold=True)
    endpoint_server = LocalEndpointServer(
        identity=LocalIdentity.load(identity.directory),
        settings=EndpointSettings(port=0, timeout_s=30.0),
    )
    endpoint_server.start()
    connection = _connect(endpoint_server)
    response = _send(connection, "POST", "/v1/chat/completions", _chat(stream=True))
    try:
        assert len(_read_frames(response, limit=2)) == 2, "the answer had started"
    finally:
        # Closing the *response* is what closes the socket here: the endpoint
        # answers a stream with `Connection: close`, on which `http.client`
        # drops its own socket reference after reading the head.
        response.close()
        connection.close()
    try:
        # Increments nobody is left to read: writing one to a closed socket is how
        # the endpoint discovers the caller is gone.
        for index in range(3):
            server.emit(f"an increment nobody will read {index}")
        _wait_until(lambda: bool(server.cancels))
        assert set(server.cancels) == {server.last.request_id}

        logged = ""

        def _log_says_the_caller_left() -> bool:
            nonlocal logged
            logged += capsys.readouterr().out
            return "client_gone" in logged

        _wait_until(_log_says_the_caller_left)
    finally:
        endpoint_server.stop(grace_s=2.0)


def test_a_second_request_on_one_connection_is_answered_on_its_own(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """Keep-alive must not leak one request's state into the next.

    An agent loop holds one connection open across many calls, so a handler that
    remembered it had already answered would silently drop the second one.
    """
    server.answer_with(deltas=DELTAS)
    endpoint_server = endpoint()
    connection = _connect(endpoint_server)
    try:
        first = _send(connection, "POST", "/v1/chat/completions", _chat())
        first_body = json.loads(first.read())
        second = _send(connection, "POST", "/v1/chat/completions", _chat())
        second_body = json.loads(second.read())
    finally:
        connection.close()

    assert first.status == second.status == 200
    assert first_body["choices"][0]["message"]["content"] == ANSWER
    assert second_body["choices"][0]["message"]["content"] == ANSWER
    assert first_body["id"] != second_body["id"]
    assert len(server.admitted) == 2


# --------------------------------------------------------------------------- #
# What must never leave the machine
# --------------------------------------------------------------------------- #


def test_no_answer_header_body_or_log_line_carries_the_signing_key(
    server: FakeServer,
    identity: Identity,
    endpoint: Callable[..., LocalEndpointServer],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The key is read in order to sign, and is part of nothing rendered.

    Asserted over every surface the endpoint writes to — the whole response,
    every header, and its own stdout — because a key that leaks into an access
    log is a key that leaked.
    """
    server.answer_with(deltas=DELTAS, billed=1234, split=(1100, 134), receipt_id=RECEIPT)
    endpoint_server = endpoint()
    surfaces: list[bytes] = []
    for method, path, document in (
        ("POST", "/v1/chat/completions", _chat()),
        ("GET", "/v1/models", None),
        ("POST", "/v1/chat/completions", _chat(temperature=0.7)),
    ):
        _status, headers, _body, raw = _call(endpoint_server, method, path, document)
        surfaces += [raw, str(headers).encode()]
    connection = _connect(endpoint_server)
    try:
        response = _send(connection, "POST", "/v1/chat/completions", _chat(stream=True))
        surfaces += [response.read(), str(response.headers).encode()]
    finally:
        connection.close()
    surfaces.append(capsys.readouterr().out.encode())

    secret = identity.private_key.encode()
    assert len(secret) > 20, "the fixture really holds a key"
    for surface in surfaces:
        assert secret not in surface


def test_the_endpoint_serves_a_tool_that_sends_a_placeholder_key(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """No credential is checked, and the placeholder the emitter documents works.

    Stated as a test rather than left implicit: a client that cannot send a
    non-empty credential will not talk at all, so the documented placeholder has
    to be accepted — and so has any other value, which is the honest description
    of what loopback binding does and does not buy.
    """
    server.answer_with(deltas=DELTAS)
    endpoint_server = endpoint()
    connection = _connect(endpoint_server)
    try:
        response = _send(
            connection,
            "POST",
            "/v1/chat/completions",
            _chat(),
            headers={"authorization": f"Bearer {LOCAL_API_KEY_PLACEHOLDER}"},
        )
        body = json.loads(response.read())
        status = response.status
    finally:
        connection.close()

    assert status == 200
    assert body["choices"][0]["message"]["content"] == ANSWER


def test_a_request_the_endpoint_cannot_sign_is_a_local_error_not_an_upstream_one(
    tmp_path: Path, server: FakeServer
) -> None:
    """A key that will not sign is reported as this machine's problem.

    Nothing was submitted, so nothing can be billed, and saying so is cheaper for
    the operator than a ``502`` that points at the pool.
    """
    identity = _write_identity(tmp_path / "broken", server.url)
    broken = dict(identity.key_document)
    broken["private_key_b64"] = base64_encode(b"too short to be a key")
    (identity.directory / "key.json").write_bytes(canonicalize(broken))
    endpoint_server = LocalEndpointServer(
        identity=LocalIdentity.load(identity.directory),
        settings=EndpointSettings(port=0),
    )
    endpoint_server.start()
    try:
        status, _headers, document, _raw = _call(
            endpoint_server, "POST", "/v1/chat/completions", _chat()
        )
    finally:
        endpoint_server.stop(grace_s=1.0)

    assert status == 500
    assert document["error"]["code"] == "internal_error"
    assert "could not sign this request" in document["error"]["message"]
    assert str(identity.directory) in document["error"]["message"]
    assert server.admitted == []


# --------------------------------------------------------------------------- #
# The configuration a tool is handed, used against the endpoint that serves it
# --------------------------------------------------------------------------- #


def test_the_emitted_configuration_is_a_configuration_that_works(
    server: FakeServer,
    endpoint: Callable[..., LocalEndpointServer],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Emit, then use: what ``config emit`` prints is a working configuration.

    Both families, against the endpoint that is really listening. The two base
    URLs differ by one path segment — an OpenAI client appends
    ``chat/completions`` to what it is given, Claude Code appends ``/v1/messages``
    itself — and the difference is only visible by making the request each block
    describes. The routes are derived from the emitted strings rather than written
    down again, so this test cannot pass on a configuration that a tool would
    read differently.
    """
    server.answer_with(deltas=DELTAS)
    endpoint_server = endpoint()

    assert main(["config", "emit", "--shell", "--port", str(endpoint_server.port)]) == 0
    shell_block = capsys.readouterr().out
    assert main(
        ["config", "emit", "--claude-code", "--port", str(endpoint_server.port), "--model", MODEL]
    ) == 0
    claude_document = json.loads(capsys.readouterr().out)

    exported: dict[str, str] = {}
    for line in shell_block.splitlines():
        if not line.startswith("export "):
            continue
        name, _, value = line.removeprefix("export ").partition("=")
        exported[name] = value.strip('"')
    openai_base = exported["OPENAI_BASE_URL"]
    anthropic_base = claude_document["env"]["ANTHROPIC_BASE_URL"]
    assert openai_base == endpoint_server.base_url, "the emitter and the endpoint agree"
    assert anthropic_base == endpoint_server.anthropic_base_url
    assert exported["OPENAI_API_KEY"] == LOCAL_API_KEY_PLACEHOLDER

    openai_route = f"{urlsplit(openai_base).path}/chat/completions"
    status, _headers, document, _raw = _call(endpoint_server, "POST", openai_route, _chat())
    assert status == 200
    assert document["choices"][0]["message"]["content"] == ANSWER

    anthropic_route = f"{urlsplit(anthropic_base).path}/v1/messages"
    status, _headers, document, _raw = _call(
        endpoint_server,
        "POST",
        anthropic_route,
        {"model": MODEL, "max_tokens": 64,
         "messages": [{"role": "user", "content": "what settles a request?"}]},
    )
    assert status == 200
    assert document["content"][0]["text"] == ANSWER
    assert claude_document["env"]["ANTHROPIC_MODEL"] == MODEL

    status, _headers, document, _raw = _call(
        endpoint_server, "GET", f"{urlsplit(openai_base).path}/models"
    )
    assert status == 200
    assert [model["id"] for model in document["data"]] == [MODEL]
    assert len(server.admitted) == 2, "one request per completion route, none for the list"


# --------------------------------------------------------------------------- #
# P0-2: a visited web page must not reach the signing path
# --------------------------------------------------------------------------- #


def test_a_cross_origin_post_is_refused_before_any_spend(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """A browser visiting an evil page fires a CORS simple request here.

    The endpoint signs as this machine's identity and spends its credit, so a
    cross-origin ``Origin`` is refused with ``403`` before any routing or spend:
    the server never admitted the request.
    """
    endpoint_server = endpoint()
    connection = _connect(endpoint_server)
    try:
        response = _send(
            connection,
            "POST",
            "/v1/chat/completions",
            _chat(),
            headers={"Origin": "https://evil.example"},
        )
        document = json.loads(response.read())
        status = response.status
    finally:
        connection.close()

    assert status == 403
    assert document["error"]["code"] == "forbidden_origin"
    assert server.admitted == [], "the visited page spent nothing"


def test_a_cross_origin_get_is_refused_too(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """The model list is the same loopback surface and gets the same guard."""
    endpoint_server = endpoint()
    connection = _connect(endpoint_server)
    try:
        response = _send(
            connection, "GET", "/v1/models", headers={"Origin": "https://evil.example"}
        )
        document = json.loads(response.read())
        status = response.status
    finally:
        connection.close()

    assert status == 403
    assert document["error"]["code"] == "forbidden_origin"
    assert server.admitted == []


def test_a_text_plain_body_is_refused_not_forwarded(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """A browser simple request arrives as ``text/plain`` with no preflight.

    Requiring ``application/json`` forces that request through a preflight the
    endpoint answers with ``405``, so the browser blocks it. The body is refused
    with ``415`` here, before anything is signed or submitted.
    """
    endpoint_server = endpoint()
    connection = _connect(endpoint_server)
    try:
        response = _send(
            connection,
            "POST",
            "/v1/chat/completions",
            _chat(),
            headers={"content-type": "text/plain"},
        )
        document = json.loads(response.read())
        status = response.status
    finally:
        connection.close()

    assert status == 415
    assert document["error"]["code"] == "invalid_request"
    assert server.admitted == [], "an unsigned, un-forwarded body costs nothing"


def test_a_preflight_options_is_refused(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """Answering an ``OPTIONS`` preflight would let a cross-origin request through.

    The endpoint serves no cross-origin surface, so the preflight is refused with
    ``405`` and the browser blocks the actual request.
    """
    endpoint_server = endpoint()
    connection = _connect(endpoint_server)
    try:
        connection.request(
            "OPTIONS",
            "/v1/chat/completions",
            headers={"content-type": "application/json"},
        )
        response = connection.getresponse()
        status = response.status
        response.read()
    finally:
        connection.close()

    assert status == 405


def test_a_same_machine_request_without_origin_is_unaffected(
    server: FakeServer, endpoint: Callable[..., LocalEndpointServer]
) -> None:
    """The guard is an allowlist: a local tool sends no ``Origin`` and is served.

    Pinned explicitly so a future change cannot turn the guard into a blanket
    refusal of the only callers the endpoint exists for.
    """
    server.answer_with(deltas=DELTAS)
    endpoint_server = endpoint()

    status, _headers, document, _raw = _call(
        endpoint_server, "POST", "/v1/chat/completions", _chat()
    )

    assert status == 200
    assert document["choices"][0]["message"]["content"] == ANSWER
    assert len(server.admitted) == 1


# --------------------------------------------------------------------------- #
# P1-11: the local proxy must bound a client connection with a socket read timeout
# --------------------------------------------------------------------------- #


def test_the_endpoint_handler_configures_a_socket_read_timeout(
    identity: Identity,
) -> None:
    """A stalled or idle client must not pin a worker thread forever.

    `ThreadingHTTPServer` opens one thread per connection and caps none, so a
    slow read would occupy a worker until the process is killed. The handler
    sets `timeout`, which the stdlib's `StreamRequestHandler.setup` applies to
    the client socket, cutting a slow read off rather than awaiting it.
    """
    handler = _handler_class(
        identity=LocalIdentity.load(identity.directory),
        settings=EndpointSettings(),
        in_flight=_InFlight(),
        session_id=new_session_id(),
    )

    assert handler.timeout == LOCAL_ENDPOINT_SOCKET_TIMEOUT_S
    assert LOCAL_ENDPOINT_SOCKET_TIMEOUT_S > 0


def test_a_stalled_client_connection_is_closed_after_the_read_timeout(
    server: FakeServer,
    endpoint: Callable[..., LocalEndpointServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that connects but never sends is dropped, not held.

    The production timeout (60s) is too long to wait on, so the module constant
    is shortened for this test only; the handler reads it when it is built, so the
    endpoint that comes up here carries the short bound. The harness's own socket
    exposes the raw connection, so we can connect, send nothing, and confirm the
    server closes the socket rather than holding a thread on it.

    The assertion is on *when* the connection closes: if the server's read
    timeout fired it drops the socket within the short bound, well before the
    client's own 5s ceiling, so a held-open connection (which would only close
    when the client gives up) cannot pass.
    """
    import aipotluck.localapi as localapi

    monkeypatch.setattr(localapi, "LOCAL_ENDPOINT_SOCKET_TIMEOUT_S", 1.0)
    endpoint_server = endpoint()

    connection = _connect(endpoint_server)
    connection.connect()
    try:
        connection.sock.settimeout(5.0)
        started = time.monotonic()
        closed = False
        while True:
            try:
                chunk = connection.sock.recv(4096)
            except OSError:
                closed = True
                break
            if not chunk:
                closed = True
                break
        elapsed = time.monotonic() - started
    finally:
        connection.close()

    assert closed, "the server closed the idle connection"
    assert elapsed < 3.0, f"closed by the server's read timeout, not the client ({elapsed:.1f}s)"
    assert len(server.admitted) == 0, "an unsent request was never parsed or forwarded"


# --------------------------------------------------------------------------- #
# P2: a caller-controlled model string must not forge a log line
# --------------------------------------------------------------------------- #


def test_a_model_with_control_characters_is_not_logged_verbatim(
    server: FakeServer,
    endpoint: Callable[..., LocalEndpointServer],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A model carrying newlines/CR must never appear as its own log line.

    The model is attacker-controlled and is printed in the request summary. A
    value that is not one readable token is replaced with `<invalid>` in the log
    (the same rule `wire.check_session_id` enforces), so it cannot forge a line
    or hide an entry. It still travels in the signed request unaltered.
    """
    server.answer_with(deltas=DELTAS)
    endpoint_server = endpoint()
    bad_model = "good\r\nFAKE LOG LINE\r\nmodel=pwned"

    _call(endpoint_server, "POST", "/v1/chat/completions", _chat(model=bad_model))

    logged = ""

    def _log_present() -> bool:
        nonlocal logged
        logged += capsys.readouterr().out
        return "model=" in logged

    _wait_until(_log_present)
    # The malicious payload must never surface as a forged line.
    assert "FAKE LOG LINE" not in logged
    assert "model=pwned" not in logged
    assert "model=<invalid>" in logged
    # The odd model still reached the server unchanged (only the log is sanitized).
    assert len(server.admitted) == 1
    assert server.admitted[0]["model"] == bad_model


def test_a_legal_model_is_logged_verbatim(
    server: FakeServer,
    endpoint: Callable[..., LocalEndpointServer],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A model that is one readable token is logged as the caller named it.

    Pinned so the sanitizer cannot silently drop or mangle a legitimate name
    (slashes and colons are part of real model ids and pass the token rule).
    """
    server.answer_with(deltas=DELTAS)
    endpoint_server = endpoint()

    _call(
        endpoint_server,
        "POST",
        "/v1/chat/completions",
        _chat(model="coding-pool/v1:2"),
    )

    logged = ""

    def _log_present() -> bool:
        nonlocal logged
        logged += capsys.readouterr().out
        return "model=" in logged

    _wait_until(_log_present)
    assert "model=coding-pool/v1:2" in logged

