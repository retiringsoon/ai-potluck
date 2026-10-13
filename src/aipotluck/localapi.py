"""A loopback-only endpoint that speaks the tools' shapes and signs as this machine.

What this is
------------
``potluck serve-openai`` runs a small HTTP server on this machine that accepts the
request shapes tools already speak — OpenAI's ``POST /v1/chat/completions`` and
``GET /v1/models``, and Anthropic's ``POST /v1/messages``, which is what Claude
Code speaks — and turns each one into a request on the platform's own signed
consumer path. It signs a spend authorization with the identity ``potluck join``
saved in the config directory, submits the caller's conversation *verbatim*,
waits for the answer exactly the way ``potluck ask`` waits, and renders that
answer back in the caller's own format. A tool therefore needs one ``base_url``
and no potluck-specific code, and the signing key never leaves this machine.
That last part is the difference from the server's own OpenAI-compatible
surface, where the server holds bounded spending authority over a key it issued.

Threat model: loopback reachability *is* the model
-------------------------------------------------
The endpoint binds an IPv4 loopback literal and refuses to start on anything
else — ``0.0.0.0``, ``::``, a public address and a hostname are all refused, a
hostname because it is resolved through host files this process does not own. It
checks no API key: any process able to open a loopback port can spend this
identity's credit. That is stated rather than papered over, because any such
process can equally run ``potluck ask`` against the same key file, so a key check
here would add a prompt and no boundary. What loopback binding does buy is the
property that matters: the endpoint is not reachable from another machine, so the
set of processes that can spend it is the set that already could. No credential
this endpoint reads is ever written into a response, a header, or a log line.

The loopback argument does *not* hold for a web page the user happens to
visit: a browser cannot open a port or read the key file, but it can fire a
CORS simple request at ``127.0.0.1:8787`` without the visitor's knowledge.
Two guards close that gap. A request carrying a non-loopback ``Origin`` is
refused with ``403`` before any routing or spend — a local tool or ``curl``
sends no ``Origin`` and is unaffected. And the body must arrive as
``Content-Type: application/json``: a content type outside the browser's
simple-request safelist forces a preflight ``OPTIONS`` this endpoint answers
with ``405``, so the browser blocks the actual request. Together they mean a
visited page cannot reach the signing path.

Billing honesty
---------------
An answer is rendered as a completion only for a request that reached
``SUCCEEDED``. Everything else — a request the server ended in failure, one whose
wait window closed while it was still running, one cancelled because the caller
hung up — is rendered as an error document that names the platform's request id,
carries the server's own reason verbatim when it recorded one, and points at
``potluck verify receipt``. A token count is rendered only from what the platform
reported: when it reported nothing the ``usage`` block is omitted, because zeros
in a billing document read as "this was free".

Graceful exit: ``SIGINT``/``SIGTERM`` stop the listener, set the cancel flag of
every request in flight, and give those requests a short grace to tell the server
to stop the work — so an interrupted endpoint does not leave an upstream
generating tokens for an answer nobody will read.

Reuse, not a second implementation
----------------------------------
The submission body, the authorization signature and both wait loops are the ones
:mod:`aipotluck.cli` defines for ``ask``; this module drives them through the
presenter seam they already expose. A second copy of the reconnect-from-cursor
loop is how a stream ends up splicing two answers together, so there is one.
:mod:`aipotluck.cli` imports this module inside its command handler for the same
reason in reverse: the dependency runs one way.
"""

from __future__ import annotations

import contextlib
import http.server
import ipaddress
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from . import openai_wire, ux
from .canonical import canonicalize
from .cli import (
    ASK_DEFAULT_MAX_CHARGE,
    ASK_DEFAULT_TIMEOUT_S,
    ASK_NO_NODE_GRACE_S,
    PROG,
    AnswerPresenter,
    NoAvailableNodes,
    WaitDeadline,
    build_signed_request,
    install_stop_handlers,
    load_joined_identity,
    post_cancel,
    run_buffered_answer,
    run_streaming_answer,
    signed_client_for,
)
from .deploy import SignedClient
from .errors import PotluckError, ProtocolError
from .statemachine import TERMINAL_REQUEST_STATES
from .wire import ChatMessage, new_session_id

#: The address the endpoint binds unless told otherwise. A literal rather than
#: ``localhost``: a name is resolved through host files this process does not
#: control, and "resolve, then hope" is not a security invariant.
LOOPBACK_HOST: Final = "127.0.0.1"

#: The port ``potluck serve-openai`` listens on by default.
DEFAULT_PORT: Final = 8787

#: The value a tool must be given for its ``api_key`` even though this endpoint
#: checks none. A tool that cannot send a non-empty credential will not talk at
#: all, and a placeholder that names itself is harder to mistake for a secret
#: than an empty string or a real-looking key.
LOCAL_API_KEY_PLACEHOLDER: Final = "potluck-local-signing"

#: How long a stop waits for in-flight requests to wind down after cancelling
#: them. A cancel has to reach the server over the network, so this bounds the
#: wait rather than promising an outcome: the listener is already closed, and
#: what is being waited for is the cancel, not the answer.
SHUTDOWN_GRACE_S: Final = 2.0

#: Response headers naming the platform's own handles for one answer. A tool
#: ignores headers it does not know, and these are what let an operator take an
#: answer they were just served and verify the charge behind it.
REQUEST_ID_HEADER: Final = "x-potluck-request-id"
RECEIPT_ID_HEADER: Final = "x-potluck-receipt-id"

#: The routes this endpoint serves, named once so a refusal can list them.
SERVED_ROUTES: Final = (
    "POST /v1/chat/completions",
    "GET /v1/models",
    "POST /v1/messages",
)

#: The server's own refusal codes as this endpoint's HTTP status and error code.
#: A code with no entry answers ``502 upstream_failed``: the platform refused the
#: work and this endpoint cannot say more precisely why than the message it
#: forwards. Mapped by code rather than by HTTP status because the code is the
#: server's claim about *why*, and a status is a coarse class.
_SERVER_REFUSALS: Final[dict[str, tuple[int, str]]] = {
    "insufficient_funds": (429, "insufficient_quota"),
    "forbidden": (401, "invalid_api_key"),
    "unauthorized": (401, "invalid_api_key"),
    "key_revoked": (401, "invalid_api_key"),
    "invalid_token": (401, "invalid_api_key"),
    "identity_binding": (401, "invalid_api_key"),
    "bad_request": (400, "invalid_request"),
    "no_nodes": (404, "model_not_found"),
    "timeout": (504, "deadline_passed"),
}


class LocalApiError(PotluckError):
    """Raised when the local endpoint cannot be started."""


class NonLoopbackBindError(LocalApiError):
    """Raised for a bind address that is not an IPv4 loopback literal.

    A distinct type rather than a message match: refusing to become reachable
    from another machine is this endpoint's one security invariant, and a caller
    must be able to tell it apart from a port that was merely busy.
    """


def require_loopback(host: str) -> str:
    """Validate a bind address against the loopback-only invariant.

    Args:
        host: The address a caller asked to bind.

    Returns:
        The same address, as an IPv4 literal.

    Raises:
        NonLoopbackBindError: If the address is not an IPv4 loopback literal.
            ``0.0.0.0`` and ``::`` are refused because they mean every interface,
            a hostname is refused because resolving it is not something this
            process controls, and ``::1`` is refused because this listener is an
            IPv4 one — a second address family is a second way to be wrong.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise NonLoopbackBindError(
            f"refusing to bind {host!r}: this endpoint binds an IPv4 loopback "
            "address only, and a hostname is resolved through host files this "
            f"process does not control ({LOOPBACK_HOST} is the default)"
        ) from exc
    if address.version != 4 or not address.is_loopback:
        raise NonLoopbackBindError(
            f"refusing to bind {host!r}: this endpoint is reachable from this "
            "machine only, so it binds an IPv4 loopback address such as "
            f"{LOOPBACK_HOST}; binding anything else would publish an identity "
            "that can spend credit to every host able to reach this one"
        )
    return str(address)


@dataclass(frozen=True)
class LocalIdentity:
    """The consumer identity one endpoint signs with.

    Loaded once at start-up rather than per request: an identity that disappears
    mid-run should not change what a request costs or how it fails, and the key
    file is not a document to re-read for every call an agent loop makes.

    Attributes:
        config_dir: Where the identity lives, named in the verify guidance.
        config_document: The saved ``config.json``.
        key_document: The saved ``key.json``; never rendered.
        client: The signed consumer for the server this identity joined.
    """

    config_dir: Path
    config_document: dict[str, Any]
    key_document: dict[str, Any]
    client: SignedClient

    @property
    def subject_id(self) -> str:
        """Return the consumer this endpoint spends as."""
        return str(self.config_document["subject_id"])

    @classmethod
    def load(cls, config_dir: Path) -> LocalIdentity:
        """Load the identity a ``join`` saved in `config_dir`.

        Raises:
            PotluckError: If no identity is saved there, or it is malformed.
        """
        config_document, key_document = load_joined_identity(config_dir)
        return cls(
            config_dir=config_dir,
            config_document=config_document,
            key_document=key_document,
            client=signed_client_for(config_document, key_document),
        )


@dataclass(frozen=True)
class EndpointSettings:
    """Where one endpoint listens, and the terms it signs each request with.

    Attributes:
        host: The bind address; `require_loopback` accepts loopback only.
        port: The bind port. ``0`` asks the operating system for a free one,
            which is what a test uses rather than fighting over the default.
        max_charge: The charge ceiling signed into every authorization.
        timeout_s: How long one request waits for a terminal state.
        no_node_grace_s: How long an empty node list is tolerated before a
            request is reported as unroutable instead of waiting out the budget.
    """

    host: str = LOOPBACK_HOST
    port: int = DEFAULT_PORT
    max_charge: int = ASK_DEFAULT_MAX_CHARGE
    timeout_s: float = ASK_DEFAULT_TIMEOUT_S
    no_node_grace_s: float = ASK_NO_NODE_GRACE_S


class ToolShape(Protocol):
    """The seam between one platform answer and the wire format a tool reads.

    The platform interaction — sign, submit, wait, read the settlement — is
    written once; what differs between tool families is only how an answer, an
    increment or a failure is spelled. Both implementations are total: every
    method renders, and neither holds state beyond the identity of the one answer
    it was built for.
    """

    #: How the request log names this surface.
    label: str

    def usage(
        self, status: Mapping[str, Any], split: Mapping[str, int] | None
    ) -> dict[str, int] | None:
        """Return the token counts to render, or `None` to omit them."""
        ...

    def buffered_document(
        self, *, content: str, usage: Mapping[str, int] | None
    ) -> dict[str, Any]:
        """Return the one document a buffered answer is."""
        ...

    def open_frames(self) -> list[bytes]:
        """Return the frames that open a stream, before its first increment."""
        ...

    def delta_frame(self, text: str) -> bytes:
        """Return one increment as a stream frame."""
        ...

    def close_frames(self, *, usage: Mapping[str, int] | None) -> list[bytes]:
        """Return the frames that end a stream which produced an answer."""
        ...

    def error_frames(self, message: str, code: str, request_id: str | None) -> list[bytes]:
        """Return a failure as the last frames of a stream that already started."""
        ...


class OpenAiShape:
    """Renders one answer in OpenAI's chat-completions shapes."""

    label = "openai"

    def __init__(self, *, model: str, include_usage: bool = False) -> None:
        """Fix the identity every frame of this answer will carry.

        Args:
            model: The model the caller named.
            include_usage: Whether the caller asked for a terminal usage frame,
                which is what ``stream_options.include_usage`` means.
        """
        self._completion = openai_wire.Completion.new(model)
        self._include_usage = include_usage

    @property
    def completion_id(self) -> str:
        """Return the completion id this answer renders under."""
        return self._completion.id

    def usage(
        self, status: Mapping[str, Any], split: Mapping[str, int] | None
    ) -> dict[str, int] | None:
        """Return OpenAI's ``usage`` block, or `None` when nothing was reported."""
        return openai_wire.openai_usage(status, split)

    def buffered_document(
        self, *, content: str, usage: Mapping[str, int] | None
    ) -> dict[str, Any]:
        """Return the ``chat.completion`` document for a complete answer."""
        return openai_wire.render_completion(self._completion, content=content, usage=usage)

    def open_frames(self) -> list[bytes]:
        """Return the role-only chunk that opens an OpenAI stream."""
        return [
            openai_wire.sse_data(
                openai_wire.render_chunk(self._completion, delta={"role": "assistant"})
            )
        ]

    def delta_frame(self, text: str) -> bytes:
        """Return one increment as a ``chat.completion.chunk`` frame."""
        return openai_wire.sse_data(
            openai_wire.render_chunk(self._completion, delta={"content": text})
        )

    def close_frames(self, *, usage: Mapping[str, int] | None) -> list[bytes]:
        """Return the finish frame, the usage frame when asked for, and ``[DONE]``."""
        frames = [
            openai_wire.sse_data(
                openai_wire.render_chunk(
                    self._completion, finish_reason=openai_wire.FINISH_REASON_COMPLETE
                )
            )
        ]
        if self._include_usage and usage is not None:
            frames.append(
                openai_wire.sse_data(openai_wire.render_chunk(self._completion, usage=usage))
            )
        frames.append(openai_wire.SSE_DONE)
        return frames

    def error_frames(self, message: str, code: str, request_id: str | None) -> list[bytes]:
        """Return an error frame and the terminator a stream must still end with."""
        return [
            openai_wire.sse_data(
                openai_wire.render_error(message, code, request_id=request_id)
            ),
            openai_wire.SSE_DONE,
        ]


class AnthropicShape:
    """Renders one answer in Anthropic's messages shapes.

    The event sequence is the documented one — ``message_start``,
    ``content_block_start``, one ``content_block_delta`` per increment,
    ``content_block_stop``, ``message_delta``, ``message_stop`` — because a client
    that accumulates a streamed message opens its text block from
    ``content_block_start``, and a stream that omits it leaves that client with no
    block to append to.
    """

    label = "anthropic"

    def __init__(self, *, model: str) -> None:
        """Fix the message id every event of this answer will carry.

        Args:
            model: The model the caller named.
        """
        self._model = model
        self._message_id = openai_wire.new_anthropic_message_id()

    @property
    def message_id(self) -> str:
        """Return the message id this answer renders under."""
        return self._message_id

    def usage(
        self, status: Mapping[str, Any], split: Mapping[str, int] | None
    ) -> dict[str, int] | None:
        """Return Anthropic's ``usage`` block, or `None` when no split exists."""
        del status  # Anthropic's shape has no total field to read one from
        return openai_wire.anthropic_usage(split)

    def buffered_document(
        self, *, content: str, usage: Mapping[str, int] | None
    ) -> dict[str, Any]:
        """Return the buffered ``message`` document for a complete answer."""
        return openai_wire.render_anthropic_message(
            message_id=self._message_id,
            model=self._model,
            content=content,
            usage=usage,
        )

    def open_frames(self) -> list[bytes]:
        """Return the ``message_start`` and ``content_block_start`` events."""
        return [
            openai_wire.anthropic_sse(
                openai_wire.anthropic_message_start(
                    message_id=self._message_id, model=self._model, usage=None
                )
            ),
            openai_wire.anthropic_sse(openai_wire.anthropic_block_start()),
        ]

    def delta_frame(self, text: str) -> bytes:
        """Return one increment as a ``content_block_delta`` event."""
        return openai_wire.anthropic_sse(openai_wire.anthropic_text_delta(text))

    def close_frames(self, *, usage: Mapping[str, int] | None) -> list[bytes]:
        """Return the block stop, the message delta and the terminal event."""
        output_tokens = usage.get("output_tokens") if usage else None
        return [
            openai_wire.anthropic_sse(openai_wire.anthropic_block_stop()),
            openai_wire.anthropic_sse(
                openai_wire.anthropic_message_delta(
                    output_tokens=output_tokens if isinstance(output_tokens, int) else None
                )
            ),
            openai_wire.anthropic_sse(openai_wire.anthropic_message_stop()),
        ]

    def error_frames(self, message: str, code: str, request_id: str | None) -> list[bytes]:
        """Return the error event that ends a stream which already started."""
        return [
            openai_wire.anthropic_sse(
                openai_wire.render_anthropic_error(message, code, request_id=request_id)
            )
        ]


class ResponseWriter(Protocol):
    """The part of a handler a streaming sink writes through.

    A protocol so the sinks depend on two methods rather than on
    ``BaseHTTPRequestHandler``, and so a test can drive them with a recorder
    instead of a socket.
    """

    def begin_stream(self, frames: Sequence[bytes]) -> None:
        """Send the streaming response head and the opening frames, once."""
        ...

    def write_frame(self, frame: bytes) -> bool:
        """Write one frame, reporting whether the caller is still there."""
        ...


class CollectingSink:
    """A presenter that keeps the answer instead of writing it.

    A buffered answer is one document, so increments are collected and rendered
    once at the end. This is the seam ``ask``'s own presenters implement, which is
    what lets the endpoint reuse that command's wait loops.
    """

    def __init__(self) -> None:
        """Create a sink with an empty accumulator."""
        self.parts: list[str] = []

    def status(self, text: str) -> None:
        """Ignore a status line: a tool reads the answer document, not progress."""
        del text

    def delta(self, text: str) -> None:
        """Keep one increment."""
        self.parts.append(text)

    def finish(self, *, answer: str, cost: str, status: str, request_id: str) -> None:
        """Ignore the completion notice; the endpoint renders its own document."""
        del answer, cost, status, request_id


class StreamingSink:
    """A presenter that writes each increment to a tool's stream as it arrives.

    The first increment is what opens the response: until then the status line and
    headers are held, so a request that fails before producing anything is
    answered with its real status rather than with a ``200`` carrying an error
    inside a stream.
    """

    def __init__(self, writer: ResponseWriter, shape: ToolShape) -> None:
        """Bind the sink to the response it writes and the format it writes in.

        Args:
            writer: The response writer, which owns the deferred headers.
            shape: The wire format one increment is rendered in.
        """
        self._writer = writer
        self._shape = shape

    def status(self, text: str) -> None:
        """Ignore a status line: a stream carries frames, not progress notices."""
        del text

    def delta(self, text: str) -> None:
        """Write one increment, opening the stream if it is the first."""
        if not text:
            return
        self._writer.begin_stream(self._shape.open_frames())
        self._writer.write_frame(self._shape.delta_frame(text))

    def finish(self, *, answer: str, cost: str, status: str, request_id: str) -> None:
        """Ignore the completion notice; the endpoint renders its own frames."""
        del answer, cost, status, request_id


class _InFlight:
    """The requests one running endpoint has open, so a stop can cancel them.

    A cancel is posted to the server rather than merely abandoned locally: the
    server pushes it on to the node running the work, so the upstream call stops
    instead of billing to its end for an answer nobody will read.
    """

    def __init__(self) -> None:
        """Create an empty register."""
        self._lock = threading.Lock()
        self._events: list[threading.Event] = []

    def register(self) -> threading.Event:
        """Record one request in flight and return the event that cancels it."""
        event = threading.Event()
        with self._lock:
            self._events.append(event)
        return event

    def release(self, event: threading.Event) -> None:
        """Forget one finished request."""
        with self._lock, contextlib.suppress(ValueError):
            self._events.remove(event)

    @property
    def pending(self) -> int:
        """Return how many requests are still registered."""
        with self._lock:
            return len(self._events)

    def cancel_all(self) -> int:
        """Ask every request in flight to cancel, returning how many there were."""
        with self._lock:
            events = list(self._events)
        for event in events:
            event.set()
        return len(events)

    def wait_quiet(self, grace_s: float) -> bool:
        """Wait for the register to empty, reporting whether it did.

        Args:
            grace_s: How long to wait. The listener is already closed by now, so
                the bound is on the cancels rather than on any answer.
        """
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if not self.pending:
                return True
            time.sleep(0.05)
        return not self.pending


class LocalEndpointServer:
    """The loopback listener that serves the tools' shapes.

    Construction is where the loopback invariant is enforced, so a programmatic
    caller cannot bind a published address any more than ``--host`` can. The
    listener is a ``ThreadingHTTPServer``: an agent loop may pipeline calls, and
    one slow request must not hold the port.
    """

    def __init__(self, *, identity: LocalIdentity, settings: EndpointSettings) -> None:
        """Bind the listener.

        Args:
            identity: The consumer identity every request is signed with.
            settings: The bind address and the terms each request is signed with.

        Raises:
            NonLoopbackBindError: If the bind address is not IPv4 loopback.
            LocalApiError: If the port cannot be bound.
        """
        host = require_loopback(settings.host)
        self.identity = identity
        self.settings = settings
        self.in_flight = _InFlight()
        #: The session every request this endpoint signs is attributed to. One per
        #: endpoint process, minted here rather than per request: an agent loop
        #: driven through this endpoint *is* one session, and the server
        #: aggregates a session's turns and prefers the node that served its last
        #: one. A caller that wants its own session boundaries talks to the
        #: server directly instead of through this loopback convenience.
        self.session_id = new_session_id()
        self._thread: threading.Thread | None = None
        try:
            self._httpd = http.server.ThreadingHTTPServer(
                (host, settings.port), self._handler_class()
            )
        except OSError as exc:
            raise LocalApiError(
                f"cannot listen on {host}:{settings.port}: {exc}; pass --port to "
                "choose another port"
            ) from exc
        # Defence in depth: the address the socket actually holds is checked, not
        # only the string that was asked for.
        if self.host != host:
            self._httpd.server_close()
            raise NonLoopbackBindError(
                f"refusing to serve: the socket bound {self.host!r}, not {host!r}"
            )

    @property
    def host(self) -> str:
        """Return the address the listener is bound to."""
        return str(self._httpd.server_address[0])

    @property
    def port(self) -> int:
        """Return the port the listener is bound to."""
        return int(self._httpd.server_address[1])

    @property
    def running(self) -> bool:
        """Return whether the serve thread has been started and not yet stopped."""
        return self._thread is not None

    @property
    def base_url(self) -> str:
        """Return the OpenAI-compatible ``base_url`` this endpoint serves."""
        return f"http://{self.host}:{self.port}/v1"

    @property
    def anthropic_base_url(self) -> str:
        """Return the Anthropic-compatible base URL this endpoint serves."""
        return f"http://{self.host}:{self.port}"

    def _handler_class(self) -> type[http.server.BaseHTTPRequestHandler]:
        """Return the request handler bound to this server's state.

        Built as a closure over the server rather than reaching through
        ``self.server``: the stdlib types that attribute as its own base class,
        so every use of it would need a cast.
        """
        return _handler_class(
            identity=self.identity,
            settings=self.settings,
            in_flight=self.in_flight,
            session_id=self.session_id,
        )

    def start(self) -> None:
        """Serve on a daemon thread, returning once the listener is accepting."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="potluck-local-endpoint",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, grace_s: float = SHUTDOWN_GRACE_S) -> int:
        """Stop listening, cancel in-flight work, and wait for it to wind down.

        Returns:
            The number of requests that were asked to cancel, which is what makes
            "stopped" a statement about the work and not only about the socket.
        """
        cancelled = self.in_flight.cancel_all()
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=grace_s)
            self._thread = None
        if cancelled and not self.in_flight.wait_quiet(grace_s):
            print(
                f"{PROG} serve-openai: asked the server to cancel {cancelled} in-flight "
                f"request(s); {self.in_flight.pending} did not report back within "
                f"{grace_s:g}s and may still be settling there",
                flush=True,
            )
        return cancelled

    def serve_until(self, stop: threading.Event) -> int:
        """Serve until `stop` is set, then shut down gracefully.

        The shutdown runs even when the wait is interrupted, so an endpoint that
        dies mid-run still closes its listener and asks the server to stop the
        work it had in flight.

        Args:
            stop: The event that ends the run.

        Returns:
            The number of in-flight requests the shutdown asked to cancel.
        """
        self.start()
        cancelled = 0
        try:
            stop.wait()
        finally:
            cancelled = self.stop()
        return cancelled


def _handler_class(
    *,
    identity: LocalIdentity,
    settings: EndpointSettings,
    in_flight: _InFlight,
    session_id: str,
) -> type[http.server.BaseHTTPRequestHandler]:
    """Build the request handler class for one endpoint's state.

    Args:
        identity: The identity every request is signed with.
        settings: The spending and waiting terms every request is signed with.
        in_flight: The register a shutdown cancels through.
        session_id: The session every request this endpoint signs belongs to.

    Returns:
        The handler class.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        """One tool request, from body to signed platform request to answer."""

        # HTTP/1.1 so a tool's client can hold one connection open across the
        # calls of an agent loop. Every response therefore declares its length,
        # and a stream closes the connection instead, since it has no length to
        # declare.
        protocol_version = "HTTP/1.1"

        # The state one response owns. Declared here and assigned in
        # `_begin_request` because a keep-alive connection reuses one handler
        # instance for many requests, so a constructor assignment would be both
        # too early and too late at once.
        _answered: bool
        _stream_open: bool
        _client_gone: bool
        _extra_headers: dict[str, str]
        #: The cancel flag of the request being served, while one is in flight.
        _cancel: threading.Event | None
        _started: float
        _log_status: int
        _log_request_id: str | None
        _log_model: str
        _log_outcome: str
        _log_tokens: str | None

        def handle_one_request(self) -> None:
            """Serve one request of a connection, answering a bug as a 500.

            The per-request state is reset here rather than in ``__init__``
            because a keep-alive connection reuses one handler instance for many
            requests: state left over from the previous one would let a second
            request believe it had already answered.

            An unexpected exception is printed and answered rather than allowed to
            reset the connection. A tool reading a closed socket learns only that
            it should retry, and a bug this endpoint swallows silently is a bug
            nobody reports.
            """
            self._begin_request()
            try:
                super().handle_one_request()
            except Exception as exc:
                # `getattr` rather than the attributes: a request line the stdlib
                # could not parse is exactly the case that lands here, and it is
                # the case where `command` and `path` were never assigned.
                print(
                    f"{PROG} serve-openai: internal error on "
                    f"{getattr(self, 'command', '-')} {getattr(self, 'path', '-')}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                with contextlib.suppress(Exception):
                    self._refuse(500, "internal_error", f"internal error: {exc}")
                self.close_connection = True

        def _begin_request(self) -> None:
            """Reset the state one response owns."""
            self._answered = False
            self._stream_open = False
            self._client_gone = False
            self._extra_headers = {}
            self._cancel = None
            self._started = time.monotonic()
            self._log_status = 0
            self._log_request_id = None
            self._log_model = ""
            self._log_outcome = ""
            self._log_tokens = None

        # -- the stdlib's own hooks -----------------------------------------

        def log_message(self, format: str, *args: Any) -> None:
            """Silence the default access log.

            This endpoint writes one summary line per request of its own, carrying
            the request id and the token count the default log cannot know. Two
            lines per request, one of them useless, is worse than one.
            """
            del format, args

        def do_GET(self) -> None:
            """Serve ``GET /v1/models``."""
            if not self._refuse_cross_origin():
                return
            route = _route_of(self.path)
            try:
                if route == "/v1/models":
                    self._serve_models()
                else:
                    self._refuse(
                        404,
                        "not_supported",
                        _not_supported_message(route),
                        anthropic=_is_anthropic_route(route),
                    )
            finally:
                self._log_line(f"GET {route}")

        def do_POST(self) -> None:
            """Serve the two completion routes.

            Routed by exact path, so a neighbour of a served route is refused
            rather than executed. ``POST /v1/messages/count_tokens``, which a
            Claude Code client sends to size a context, is the case this exists
            for: routed by prefix it would parse as a completion request and
            spend credit to answer a question about counting.
            """
            if not self._refuse_cross_origin():
                return
            route = _route_of(self.path)
            try:
                if route == "/v1/chat/completions":
                    self._serve_chat_completions()
                elif route == "/v1/messages":
                    self._serve_messages()
                else:
                    self._refuse(
                        404,
                        "not_supported",
                        _not_supported_message(route),
                        anthropic=_is_anthropic_route(route),
                    )
            finally:
                self._log_line(f"POST {route}")

        def do_OPTIONS(self) -> None:
            """Refuse preflight: the endpoint serves no cross-origin surface.

            A browser sends ``OPTIONS`` before a cross-origin request whose
            ``Content-Type`` is not in its simple-request safelist. Answering it
            would let such a request through, so it is refused rather than
            answered — which is what keeps the content-type guard meaningful.
            """
            self._refuse(
                405,
                "method_not_allowed",
                "OPTIONS is not served; this endpoint answers only the tools' "
                "own request shapes",
            )

        def _refuse_cross_origin(self) -> bool:
            """Return ``True`` if the request may proceed, refusing it otherwise.

            A request carrying a non-loopback ``Origin`` is a cross-origin
            browser caller: it can reach this loopback endpoint through a CORS
            simple request and spend this machine's identity credit without the
            visitor's knowledge. Such a request is refused before any routing or
            spend. Local tools and ``curl`` send no ``Origin`` and are unaffected.
            """
            origin = self.headers.get("Origin")
            if origin and not _is_loopback_origin(origin):
                self._refuse(
                    403,
                    "forbidden_origin",
                    "this endpoint serves only same-machine callers; a "
                    "cross-origin request is refused so a visited page cannot "
                    "spend this identity's credit",
                )
                return False
            return True

        # -- the routes -----------------------------------------------------

        def _serve_models(self) -> None:
            """Answer with the models the connected, sharing nodes advertise."""
            try:
                document = identity.client.get("/v1/nodes")
            except PotluckError as exc:
                self._refuse(502, "upstream_failed", _server_failure_message(exc))
                return
            nodes = document.get("nodes")
            models = openai_wire.aggregate_models(nodes if isinstance(nodes, list) else [])
            self._log_outcome = f"{len(models)} model(s)"
            self._send_json(200, openai_wire.render_model_list(models))

        def _serve_chat_completions(self) -> None:
            """Serve ``POST /v1/chat/completions``, OpenAI's shape."""
            body = self._read_body()
            if body is None:
                return
            try:
                request = openai_wire.ChatCompletionRequest.from_body(
                    openai_wire.parse_body(body)
                )
            except ProtocolError as exc:
                self._refuse(400, "invalid_request", str(exc))
                return
            self._serve_completion(
                shape=OpenAiShape(model=request.model, include_usage=request.include_usage),
                model=request.model,
                messages=request.messages,
                parameters=request.parameters,
                delivery_mode=request.delivery_mode,
            )

        def _serve_messages(self) -> None:
            """Serve ``POST /v1/messages``, Anthropic's shape."""
            body = self._read_body(anthropic=True)
            if body is None:
                return
            try:
                request = openai_wire.MessagesRequest.from_body(openai_wire.parse_body(body))
            except ProtocolError as exc:
                self._refuse(400, "invalid_request", str(exc), anthropic=True)
                return
            self._serve_completion(
                shape=AnthropicShape(model=request.model),
                model=request.model,
                messages=request.messages,
                parameters=request.parameters,
                delivery_mode=request.delivery_mode,
            )

        def _serve_completion(
            self,
            *,
            shape: ToolShape,
            model: str,
            messages: tuple[ChatMessage, ...],
            parameters: dict[str, Any],
            delivery_mode: str,
        ) -> None:
            """Sign one request, submit it, wait for it, and render the answer.

            The whole platform interaction, shared by both tool formats: `shape` is
            the only thing that differs between them, which is why there is one of
            these and not two.
            """
            self._log_model = model
            streaming = delivery_mode == "stream_strict"
            try:
                request_id, body = build_signed_request(
                    config_document=identity.config_document,
                    key_document=identity.key_document,
                    messages=messages,
                    model=model,
                    parameters=parameters,
                    max_charge=settings.max_charge,
                    delivery_mode=delivery_mode,
                    deadline_ms=int(settings.timeout_s * 1000),
                    session_id=session_id,
                )
            except PotluckError as exc:
                # Nothing was submitted, so nothing can be billed: this is a local
                # failure to sign, and saying so is cheaper than a 502.
                self._refuse(
                    500,
                    "internal_error",
                    f"the local identity in {identity.config_dir} could not sign this "
                    f"request: {exc}",
                    anthropic=shape.label == AnthropicShape.label,
                )
                return
            self._log_request_id = request_id
            self._extra_headers[REQUEST_ID_HEADER] = request_id
            cancel = in_flight.register()
            # Held where `write_frame` can reach it: a caller that hangs up
            # mid-stream is discovered there, and discovering it has to stop the
            # work rather than only stop the writing.
            self._cancel = cancel
            try:
                self._submit_and_wait(
                    shape=shape,
                    model=model,
                    body=body,
                    request_id=request_id,
                    streaming=streaming,
                    cancel=cancel,
                )
            finally:
                if cancel.is_set():
                    # Posted even when the wait loop already posted one: a cancel
                    # the server has applied is refused and ignored, and the
                    # buffered loop does not post one at all.
                    post_cancel(identity.client, request_id)
                self._cancel = None
                in_flight.release(cancel)

        def _submit_and_wait(
            self,
            *,
            shape: ToolShape,
            model: str,
            body: dict[str, Any],
            request_id: str,
            streaming: bool,
            cancel: threading.Event,
        ) -> None:
            """Submit one signed request and render however its wait ended."""
            anthropic = shape.label == AnthropicShape.label
            try:
                submitted = identity.client.post("/v1/requests", body)
            except PotluckError as exc:
                self._refuse(
                    *_server_refusal(exc),
                    _server_failure_message(exc, request_id=request_id),
                    request_id=request_id,
                    anthropic=anthropic,
                )
                return
            returned = submitted.get("request_id")
            if isinstance(returned, str) and returned:
                request_id = returned
                self._log_request_id = request_id
                self._extra_headers[REQUEST_ID_HEADER] = request_id
            sink: AnswerPresenter = (
                StreamingSink(self, shape) if streaming else CollectingSink()
            )
            wait = run_streaming_answer if streaming else run_buffered_answer
            try:
                _status, _cancelled, parts = wait(
                    identity.client,
                    request_id,
                    sink,
                    model=model,
                    timeout_s=settings.timeout_s,
                    no_node_grace_s=settings.no_node_grace_s,
                    cancel_event=cancel,
                )
            except NoAvailableNodes as exc:
                self._fail_completion(
                    shape,
                    404,
                    "model_not_found",
                    f"{exc}",
                    request_id,
                    receipt_id=None,
                )
                return
            except WaitDeadline as exc:
                # This endpoint's own wait window closed with the work still
                # unfinished. Answered as a deadline rather than as an upstream
                # failure, and with the billing-honest message: nothing was
                # delivered, the charge is not knowable from here, and the
                # request may still settle at the server.
                del exc  # `_unfinished_message` states the deadline itself
                self._fail_completion(
                    shape,
                    504,
                    "deadline_passed",
                    _unfinished_message(
                        request_id=request_id,
                        status="",
                        billing_note=None,
                        receipt_id=None,
                        config_dir=identity.config_dir,
                        timeout_s=settings.timeout_s,
                    ),
                    request_id,
                    receipt_id=None,
                )
                return
            except PotluckError as exc:
                if cancel.is_set():
                    # The wait ended because this request was asked to cancel,
                    # either by a caller that hung up or by an endpoint that is
                    # stopping. The work is cancelled in `_serve_completion`'s
                    # `finally` either way; what differs is whether anybody is
                    # left to answer.
                    self._log_outcome = "client_gone" if self._client_gone else "cancelled"
                    if self._client_gone:
                        return
                    # A socket that simply closes would leave a tool unable to
                    # tell "the endpoint went away" from "the model said
                    # nothing", so the caller still connected is answered.
                    self._fail_completion(
                        shape,
                        503,
                        "service_unavailable",
                        _stopping_message(
                            request_id=request_id,
                            config_dir=identity.config_dir,
                            timeout_s=settings.timeout_s,
                        ),
                        request_id,
                        receipt_id=None,
                    )
                    return
                status_code, code = _server_refusal(exc)
                self._fail_completion(
                    shape,
                    status_code,
                    code,
                    _server_failure_message(exc, request_id=request_id),
                    request_id,
                    receipt_id=None,
                )
                return
            self._render_answer(
                shape,
                request_id=request_id,
                content="".join(parts),
                streaming=streaming,
            )

        def _render_answer(
            self, shape: ToolShape, *, request_id: str, content: str, streaming: bool
        ) -> None:
            """Render the answer for a wait that returned rather than raised.

            Returning is not the same as succeeding: the wait loops also return for
            a request the server ended in failure, and the status document — not
            the fact that a loop finished — decides whether this is a completion or
            an error.

            The status and the token split are read back from the server rather
            than taken from the wait loop, which is one extra round trip per
            request and the price of not reimplementing the loop here: the books
            are the authority on what a request cost.
            """
            document, split = _final_facts(identity.client, request_id)
            status = str(document.get("status", ""))
            receipt_id = _receipt_id(document)
            if receipt_id:
                self._extra_headers[RECEIPT_ID_HEADER] = receipt_id
            usage = shape.usage(document, split)
            self._log_tokens = _token_summary(usage)
            if status != "SUCCEEDED":
                self._fail_completion(
                    shape,
                    *_failure_answer(status),
                    _unfinished_message(
                        request_id=request_id,
                        status=status,
                        billing_note=_billing_note(document),
                        receipt_id=receipt_id,
                        config_dir=identity.config_dir,
                        timeout_s=settings.timeout_s,
                    ),
                    request_id,
                    receipt_id=receipt_id,
                )
                return
            self._log_outcome = status
            if streaming:
                # An answer with no increments is still a complete one: open the
                # stream so the caller reads a well-formed empty completion
                # rather than a closed socket.
                self.begin_stream(shape.open_frames())
                for frame in shape.close_frames(usage=usage):
                    if not self.write_frame(frame):
                        break
                return
            self._send_json(200, shape.buffered_document(content=content, usage=usage))

        # -- response writing ----------------------------------------------

        def begin_stream(self, frames: Sequence[bytes]) -> None:
            """Send the ``200`` and the SSE head once, then the opening frames."""
            if self._stream_open or self._client_gone or self._answered:
                return
            self._stream_open = True
            self._answered = True
            self._log_status = 200
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self._send_extra_headers()
            self.end_headers()
            self.close_connection = True
            for frame in frames:
                if not self.write_frame(frame):
                    return

        def write_frame(self, frame: bytes) -> bool:
            """Write one stream frame, reporting a caller that hung up.

            A disconnect is not an error to answer — there is nobody to answer —
            but it is a reason to stop the work, so the cancel flag of this
            request is set and the wait loop asks the server to stop it: an
            upstream must not keep generating tokens for an answer nobody is
            left to read.

            Noticed on the write rather than by polling the socket, so a caller
            that hangs up during a long silence is discovered when the next
            increment arrives. That is the case which matters — it is the moment
            the work would otherwise keep being paid for — and a buffered answer,
            which writes nothing until it is complete, cannot notice at all.
            """
            if self._client_gone or not self._stream_open:
                return False
            try:
                self.wfile.write(frame)
                self.wfile.flush()
            except (OSError, ValueError):
                self._client_gone = True
                self._log_outcome = "client_gone"
                if self._cancel is not None:
                    self._cancel.set()
                return False
            return True

        def _send_extra_headers(self) -> None:
            """Send the headers naming this answer's platform handles."""
            for key, value in self._extra_headers.items():
                self.send_header(key, value)

        def _send_json(self, status: int, document: Mapping[str, Any]) -> None:
            """Answer with one JSON document, unless an answer already went out."""
            if self._answered:
                return
            self._answered = True
            self._log_status = status
            body = canonicalize(dict(document))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._send_extra_headers()
            self.end_headers()
            try:
                self.wfile.write(body)
            except (OSError, ValueError):
                self._client_gone = True
                self._log_outcome = "client_gone"

        def _refuse(
            self,
            status: int,
            code: str,
            message: str,
            *,
            request_id: str | None = None,
            anthropic: bool = False,
        ) -> None:
            """Answer with an error document in the caller's own shape."""
            if request_id:
                self._extra_headers["x-request-id"] = request_id
            render = (
                openai_wire.render_anthropic_error if anthropic else openai_wire.render_error
            )
            self._log_status = status
            # A caller that already named the outcome keeps it: "cancelled" says
            # more about what happened than the code the answer was rendered
            # with, and the log line is the only place that distinction survives.
            self._log_outcome = self._log_outcome or f"refused:{code}"
            self._send_json(status, render(message, code, request_id=request_id))

        def _fail_completion(
            self,
            shape: ToolShape,
            status: int,
            code: str,
            message: str,
            request_id: str,
            *,
            receipt_id: str | None,
        ) -> None:
            """Answer a failed completion, as a status or as a stream's last frames.

            Which of the two depends on whether an answer already started: once the
            ``200`` and the SSE head are out there is no status left to send, so the
            failure travels inside the stream — and the stream still ends with its
            terminator, because a client whose read loop waits for one would
            otherwise sit on a closed socket wondering whether more was coming.
            """
            if receipt_id:
                self._extra_headers[RECEIPT_ID_HEADER] = receipt_id
            if self._stream_open:
                for frame in shape.error_frames(message, code, request_id or None):
                    if not self.write_frame(frame):
                        break
                return
            self._refuse(
                status, code, message, request_id=request_id or None,
                anthropic=shape.label == AnthropicShape.label,
            )

        def _read_body(self, *, anthropic: bool = False) -> bytes | None:
            """Read one request body under the size cap, or refuse and return `None`.

            Refused rather than truncated: a body this endpoint only half read
            would be signed as something the caller did not send.
            """
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                self._refuse(
                    411,
                    "invalid_request",
                    "a Content-Length header is required; this endpoint does not read "
                    "a chunked request body",
                    anthropic=anthropic,
                )
                return None
            if not _is_json_content_type(self.headers.get("Content-Type", "")):
                self._refuse(
                    415,
                    "invalid_request",
                    "this endpoint requires Content-Type: application/json; a "
                    "browser simple request would not send it, so this refusal "
                    "also blocks cross-origin spend",
                    anthropic=anthropic,
                )
                return None
            try:
                length = int(raw_length)
            except ValueError:
                self._refuse(
                    400,
                    "invalid_request",
                    f"Content-Length is not a number: {raw_length!r}",
                    anthropic=anthropic,
                )
                return None
            if length < 0:
                self._refuse(
                    400,
                    "invalid_request",
                    f"Content-Length is negative: {length}",
                    anthropic=anthropic,
                )
                return None
            if length > openai_wire.MAX_BODY_BYTES:
                self._refuse(
                    413,
                    "invalid_request",
                    f"the body is {length} bytes, above this endpoint's "
                    f"{openai_wire.MAX_BODY_BYTES}-byte limit",
                    anthropic=anthropic,
                )
                return None
            try:
                return self.rfile.read(length)
            except OSError as exc:
                self._client_gone = True
                self._log_outcome = "client_gone"
                print(
                    f"{PROG} serve-openai: the caller disconnected while sending a "
                    f"body: {exc}",
                    flush=True,
                )
                return None

        def _log_line(self, route: str) -> None:
            """Print this request's one summary line.

            It names the route, the status, the model, the platform's request id
            and the token count — and never a credential. The identity's key file
            is read in order to sign and is not part of anything rendered here. A
            count the platform did not report is printed as to be confirmed rather
            than as zero, for the same reason a completion omits ``usage``.
            """
            parts = [f"{PROG} serve-openai:", route, str(self._log_status or "-")]
            if self._log_model:
                parts.append(f"model={self._log_model}")
            if self._log_request_id:
                parts.append(f"request={self._log_request_id}")
            if self._log_tokens:
                parts.append(f"tokens={self._log_tokens}")
            if self._log_outcome:
                parts.append(self._log_outcome)
            parts.append(f"{int((time.monotonic() - self._started) * 1000)}ms")
            print(" ".join(parts), flush=True)

    return Handler


def _route_of(path: str) -> str:
    """Return a request target's path, without its query."""
    return path.split("?", 1)[0]


def _is_anthropic_route(route: str) -> bool:
    """Return whether a route belongs to Anthropic's surface.

    Decided by the route even for a refusal, so a Claude Code client that asks for
    something this endpoint does not serve reads the error shape it parses.
    """
    return route.startswith("/v1/messages")


def _is_loopback_origin(origin: str) -> bool:
    """Return whether an ``Origin`` header names a loopback caller.

    A local tool talking to its own ``base_url`` sends no ``Origin`` at all; a
    page served from a loopback address sends one whose host is loopback. Any
    other origin is a cross-origin browser caller this endpoint refuses, because
    it would otherwise spend this machine's identity credit on a visit.
    """
    host = origin.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return host in ("127.0.0.1", "::1", "localhost")


def _is_json_content_type(value: str) -> bool:
    """Return whether a ``Content-Type`` header asks for JSON.

    Only ``application/json`` (an optional ``; charset=`` is stripped) is
    accepted. Requiring it forces a cross-origin browser request through a
    preflight the endpoint does not answer, which is the second half of keeping
    a visited page from reaching the signing path.
    """
    media = value.split(";", 1)[0].strip().lower()
    return media == "application/json"


def _not_supported_message(route: str) -> str:
    """Explain a route this endpoint does not serve, naming what it does."""
    return (
        f"{route} is not served here; this endpoint serves "
        + ", ".join(SERVED_ROUTES)
        + " and nothing else"
    )


def _token_summary(usage: Mapping[str, int] | None) -> str:
    """Return the token count for the log line, honest about an unknown one."""
    if usage is None:
        return ux.UNCERTAIN_USAGE_PHRASE.replace(" ", "-")
    return str(usage.get("total_tokens", usage.get("output_tokens", 0)))


def _final_facts(
    client: SignedClient, request_id: str
) -> tuple[dict[str, Any], dict[str, int] | None]:
    """Return a request's terminal status document and its reported token split.

    Both reads are best-effort. The wait has already ended, so a server that will
    not answer a follow-up read must not turn a delivered answer into an error: an
    empty document renders as "not SUCCEEDED" and the caller is given the request
    id to check, which is the honest answer when this endpoint cannot read the
    books.

    The split comes from the last ``usage.final`` event, which is where the
    contributing node's own measurement lands; a terminal request replays its
    stored events without blocking.
    """
    document: dict[str, Any] = {}
    with contextlib.suppress(PotluckError):
        document = client.get(f"/v1/requests/{request_id}")
    split: dict[str, int] | None = None
    with contextlib.suppress(PotluckError):
        for frame in client.events(request_id, 0):
            if frame.event == "usage.final":
                split = openai_wire.measurement_split(frame.payload) or split
    return document, split


def _receipt_id(document: Mapping[str, Any]) -> str | None:
    """Return the receipt a request's settlement was written under, if any."""
    settlement = document.get("settlement")
    if not isinstance(settlement, dict):
        return None
    receipt = settlement.get("receipt_id")
    return receipt if isinstance(receipt, str) and receipt else None


def _billing_note(document: Mapping[str, Any]) -> str | None:
    """Return the server's own reason for a request that did not succeed."""
    note = document.get("billing_note")
    return note if isinstance(note, str) and note else None


def _failure_answer(status: str) -> tuple[int, str]:
    """Return the HTTP status and error code for a request that produced no answer.

    Two cases, told apart by whether the server finished the request:

    * a status that is *not* terminal — including an empty one, which is what a
      server that would not answer a follow-up read leaves here — means this
      endpoint stopped waiting while the work was still unfinished. That is a
      deadline: ``504``, because the request may still be running and may still
      be billed, and reporting it as an upstream failure would claim the
      platform ended it.
    * a terminal non-success means the platform ended the request. ``EXPIRED``
      is the server's own deadline and answers ``504``; ``FAILED`` and
      ``CANCELLED`` answer ``502``, which is what "the platform did not deliver
      this" means to a caller.
    """
    if not status or status not in TERMINAL_REQUEST_STATES:
        return 504, "deadline_passed"
    if status == "EXPIRED":
        return 504, "deadline_passed"
    return 502, "upstream_failed"


def _server_refusal(exc: PotluckError) -> tuple[int, str]:
    """Map a server refusal onto this endpoint's HTTP status and error code."""
    code = getattr(exc, "error_code", None)
    if isinstance(code, str) and code in _SERVER_REFUSALS:
        return _SERVER_REFUSALS[code]
    http_status = getattr(exc, "http_status", None)
    if isinstance(http_status, int) and http_status in (401, 402, 403, 404, 429):
        return http_status, "invalid_request"
    return 502, "upstream_failed"


def _server_failure_message(exc: PotluckError, *, request_id: str = "") -> str:
    """Explain a failure of the platform itself, with the remedy the CLI gives.

    The server's own wording is carried through rather than replaced: the reason a
    request failed lives at the server, and hiding it behind "upstream failure" is
    what makes an exhausted credit balance look like an outage. The next step is
    the one :mod:`aipotluck.ux` already classifies this failure into, so this
    endpoint and ``ask`` cannot drift apart about what to do next.
    """
    guidance = ux.classify_failure(
        error_code=getattr(exc, "error_code", None),
        http_status=getattr(exc, "http_status", None),
        message=str(exc),
    )
    named = f"request {request_id}: " if request_id else ""
    return f"{named}{exc} {guidance.next_step}"


def _unfinished_message(
    *,
    request_id: str,
    status: str,
    billing_note: str | None,
    receipt_id: str | None,
    config_dir: Path,
    timeout_s: float,
) -> str:
    """Explain a request that delivered no complete answer.

    This is the message the billing-honesty rule exists for. It says three things a
    caller cannot work out for itself: that the work may still be running and may
    still be billed, that the amount is not knowable from here, and where to check
    — the receipt when one was issued, and the request id always, because that is
    what the server's own books answer against. A generic "upstream error" would
    leave a caller believing either "this was free" or "this definitely ran", and
    neither is knowable from here.
    """
    phrase = ux.describe_request(status) if status else "did not reach a terminal state"
    note = f": {billing_note}" if billing_note else ""
    window = "" if status else f" within {timeout_s:g}s"
    if receipt_id:
        check = (
            f"check what it cost with `{PROG} verify receipt --config-dir "
            f"{config_dir} --receipt-id {receipt_id}`"
        )
    else:
        check = (
            f"no receipt was issued for it; quote request {request_id} to the "
            "server's operator to have the charge confirmed"
        )
    return (
        f"request {request_id} {phrase}{note}{window}. No answer was delivered and the "
        f"charge is not knowable from here: {ux.UNCERTAIN_USAGE_PHRASE}. The work may "
        f"still be running at the server and is billed only if it settles; {check}."
    )


def _stopping_message(*, request_id: str, config_dir: Path, timeout_s: float) -> str:
    """Explain a request this endpoint cancelled because it was asked to stop.

    The caller is still connected, so it is answered with a document rather than
    with a closed socket, and the document says the two things it cannot work out
    for itself: that the work was cancelled before it produced an answer, and
    that what the cancellation cost is not knowable from here.
    """
    return "this endpoint is stopping: " + _unfinished_message(
        request_id=request_id,
        status="CANCELLED",
        billing_note=None,
        receipt_id=None,
        config_dir=config_dir,
        timeout_s=timeout_s,
    )


def serve_openai(
    *,
    settings: EndpointSettings,
    config_dir: Path,
    stop_event: threading.Event | None = None,
) -> int:
    """Load the local identity and serve the endpoint until asked to stop.

    Args:
        settings: Where to listen, and what to sign each request with.
        config_dir: Where the identity ``potluck join`` saved lives.
        stop_event: An event that ends the run. When `None`, ``SIGINT`` and
            ``SIGTERM`` are wired to one, so Ctrl-C closes the listener and
            cancels in-flight work instead of killing a handler mid-write.

    Returns:
        The process exit status: 0 on a clean stop.

    Raises:
        PotluckError: If no identity is saved in `config_dir`, or the bind address
            is not loopback. ``main`` prints it and exits non-zero, so a refusal to
            start is never a silent one.
    """
    identity = LocalIdentity.load(config_dir)
    server = LocalEndpointServer(identity=identity, settings=settings)
    installed = False
    if stop_event is None:
        stop = threading.Event()
        installed = install_stop_handlers(stop)
    else:
        stop = stop_event
    print(
        f"{PROG} serve-openai: listening on {server.host}:{server.port} "
        "(loopback only; not reachable from another machine)",
        flush=True,
    )
    print(
        f"{PROG} serve-openai: OpenAI-compatible base_url {server.base_url}; "
        f"Anthropic-compatible base_url {server.anthropic_base_url}",
        flush=True,
    )
    print(
        f"{PROG} serve-openai: signing as {identity.subject_id} with the identity in "
        f"{identity.config_dir}; no API key is checked, so any process on this machine "
        f"can spend it (send {LOCAL_API_KEY_PLACEHOLDER!r} or any other value)",
        flush=True,
    )
    print(
        f"{PROG} serve-openai: every request this run signs belongs to session "
        f"{server.session_id}; the server aggregates it, so "
        f"`potluck-server admin session-show --session {server.session_id}` reads "
        f"this run's turns, tokens and cost",
        flush=True,
    )
    print(
        f"{PROG} serve-openai: the models served are whatever the connected "
        f"contribution nodes advertise (GET {server.base_url}/models); each request "
        f"signs a ceiling of {settings.max_charge} milli-credits",
        flush=True,
    )
    if not installed and stop_event is None:
        print(
            f"{PROG} serve-openai: signal handlers could not be installed off the main "
            "thread; this run can only be ended by killing the process",
            flush=True,
        )
    try:
        cancelled = server.serve_until(stop)
    finally:
        if server.running:
            server.stop()
    if cancelled:
        print(
            f"{PROG} serve-openai: stopped; asked the server to cancel "
            f"{cancelled} in-flight request(s)",
            flush=True,
        )
    else:
        print(f"{PROG} serve-openai: stopped", flush=True)
    return 0
