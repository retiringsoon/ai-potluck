"""Minimal RFC 6455 WebSocket transport for the server's shared HTTPS/WSS port.

The server terminates HTTPS for consumers and WSS for contribution nodes on one
listening socket. Nodes dial out, so this module only needs a server-side upgrade
from inside an already-accepted `http.server.BaseHTTPRequestHandler` socket and a
client-side dial to an ``wss://`` URL. It implements exactly the framing the
project needs, and refuses anything else loudly rather than guessing.

Deliberate limitations, enforced by refusal rather than ignored:

* Text messages only. A binary frame is a protocol error; silently dropping it
  would hide a peer bug.
* No subprotocols and no extensions, including ``permessage-deflate``. A peer
  that asks for one is refused at the handshake, because answering 101 without
  honouring the negotiation leaves the peer framing bytes we cannot read.
* No fragmentation on send (each message is one frame); fragmentation on receive
  is reassembled and bounded by the reassembled size.
* No HTTP-layer integration beyond the upgrade: no cookies, no redirects, no
  proxy authentication, and no keep-alive of the pre-upgrade request.

The transport moves bytes; `aipotluck.wire` decides what they mean.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import io
import os
import select
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import NoReturn, Self, cast

from aipotluck.proxy import connect_via, resolve_proxy
from aipotluck.tls import client_ssl_context

from .errors import PotluckError

#: The RFC 6455 handshake magic string, into which the client key is hashed.
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: Largest reassembled message either side will accept, in bytes.
DEFAULT_MAX_MESSAGE_BYTES = 256 * 1024

#: How long the client waits for the server's 101 response.
DEFAULT_HANDSHAKE_TIMEOUT_S = 10.0

#: Size of a single socket read. Frames are reassembled from these chunks.
_RECV_CHUNK = 65536

#: How long `close` waits for the peer's close reply before dropping the socket.
_CLOSE_WAIT_S = 0.5

_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

_VALID_OPCODES = frozenset({_OP_CONT, _OP_TEXT, _OP_BINARY, _OP_CLOSE, _OP_PING, _OP_PONG})
_CONTROL_OPCODES = frozenset({_OP_CLOSE, _OP_PING, _OP_PONG})

#: RFC 6455 §5.5 caps a control frame's payload at 125 bytes.
_MAX_CONTROL_PAYLOAD = 125

#: Close codes reserved for local use; RFC 6455 §7.4.1 forbids them on the wire.
_RESERVED_CLOSE_CODES = frozenset({1004, 1005, 1006, 1015})

#: Header names the handshake owns; an extra header may not overwrite them.
_RESERVED_REQUEST_HEADERS = frozenset(
    {
        "host",
        "upgrade",
        "connection",
        "sec-websocket-key",
        "sec-websocket-version",
    }
)


class WebSocketError(PotluckError):
    """Base class for every transport failure on a WebSocket connection."""


class WebSocketClosed(WebSocketError):
    """Raised when the peer closed the connection.

    The transport raises this both when the peer sent a close frame and when the
    underlying socket ended without one, so a caller always learns about a closed
    connection the same way.

    Attributes:
        code: The close code the peer sent, or 1006 when the connection ended
            without a close frame.
        reason: The peer's UTF-8 close reason, empty when it sent none.
    """

    code: int
    reason: str

    def __init__(self, code: int, reason: str = "") -> None:
        detail = f"{code} {reason}".rstrip()
        super().__init__(f"websocket closed ({detail})")
        self.code = code
        self.reason = reason


class WebSocketTimeout(WebSocketError):
    """Raised when `recv_text` reaches its deadline with no complete message.

    No bytes read before the deadline are lost: the partial frame stays in the
    receive buffer, so the next `recv_text` call resumes where this one stopped.
    """


class WebSocketProtocolError(WebSocketError):
    """Raised when a frame or a handshake response violates RFC 6455.

    Every case is a refusal, never a repair: a reserved opcode, a frame masked in
    the wrong direction, a malformed control frame, a fragmented message that is
    interrupted or grows past the size limit, invalid UTF-8 in a text message
    (close code 1007), or a server handshake response that fails verification.
    """


@dataclass(frozen=True)
class HandshakeRefusal(Exception):
    """A refused upgrade, carrying the HTTP status the caller must send.

    `handshake_server` raises this instead of returning a connection. The caller
    writes the status to the client itself, because only the caller knows the
    framework's response path.

    Attributes:
        status: The HTTP status to answer with: 400, 405, 426, or 501.
        reason: Operator-facing explanation, suitable for the body.
    """

    status: int
    reason: str


@dataclass
class _Connection:
    """Socket-level machinery shared by the two public connection classes.

    Not part of the public interface. `ServerConnection` and `ClientConnection`
    are what callers construct and hold; this base holds the framing state and
    the lock so neither role duplicates it.
    """

    peer: str
    _sock: socket.socket = field(repr=False, compare=False)
    _mask_frames: bool = field(repr=False, compare=False)
    _max_message_bytes: int = field(repr=False, compare=False)
    _recv_buffer: bytearray = field(default_factory=bytearray, repr=False, compare=False)
    #: Serialises frame writes. A sender never waits for a reader, so a peer that
    #: stops reading cannot stall this side's control messages.
    _send_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    #: Serialises frame reads, making `recv_text` single-consumer. It is held for
    #: the whole blocking wait, which is why sending uses a separate lock.
    _recv_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _closed: bool = field(default=False, repr=False, compare=False)
    _close_sent: bool = field(default=False, repr=False, compare=False)
    _close_received: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Put the socket into blocking mode.

        Read deadlines are implemented with `select` rather than
        `socket.settimeout`, because a timeout is a property of the socket: a
        sender that cleared the timeout to write a large frame would otherwise
        leave a concurrent reader blocked with no deadline at all.
        """
        self._sock.setblocking(True)

    def send_text(self, text: str) -> None:
        """Send `text` as one unfragmented text frame.

        Safe to call from any number of threads: the frame is written under the
        send lock, so two senders cannot interleave a frame. It does **not** wait
        for a reader to finish, so a peer that is slow to read cannot delay this
        side's control messages — a slow output consumer must never be able to
        starve an ACK.

        Args:
            text: The message body, encoded as UTF-8.

        Raises:
            WebSocketError: If the connection has already been closed.
        """
        payload = text.encode("utf-8")
        with self._send_lock:
            if self._closed:
                raise WebSocketError("cannot send on a closed connection")
            self._send_frame(_OP_TEXT, payload)

    def recv_text(self, timeout: float | None = None) -> str:
        """Return the next text message, reassembling continuation frames.

        Reads under the read lock, which is held for the whole wait. That makes
        this method single-consumer: a second thread calling it waits behind the
        first rather than stealing half a frame. Sending is deliberately not
        serialised behind this lock.

        Control frames are handled transparently: a ping is answered with a pong
        and a pong is dropped, so neither surfaces to the caller. A close frame
        is echoed once and then reported as `WebSocketClosed`.

        Args:
            timeout: Seconds to wait for a complete message, or None to block
                until one arrives.

        Returns:
            The decoded text of the message.

        Raises:
            WebSocketTimeout: If the timeout elapses before a complete message.
            WebSocketClosed: If the peer sent a close frame or the socket ended.
            WebSocketProtocolError: On any framing violation described in the
                module docstring, including a binary frame and invalid UTF-8.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._recv_lock:
            if self._closed:
                raise WebSocketClosed(1006, "connection is closed")
            return self._read_message(deadline)

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Close the connection, attempting a clean close handshake.

        Sends a close frame, waits a bounded time (never indefinitely) for the
        peer's reply, then drops the socket. Calling it again does nothing, and a
        broken socket does not raise, so it is safe to call from a cleanup path.

        The close frame is written without waiting for a reader, so this returns
        even when another thread is blocked in `recv_text`. That thread is woken
        by the socket shutdown and sees `WebSocketClosed`, which is what makes a
        shutdown path trustworthy: a hung reader can never hold the process open.

        Args:
            code: The close code to send, within 1000..4999.
            reason: An optional UTF-8 reason, at most 123 bytes.

        Raises:
            WebSocketError: If `code` is out of range or `reason` is too long.
        """
        if self._closed:
            return
        with self._send_lock:
            if not self._close_sent:
                try:
                    self._send_close(code, reason)
                except OSError:
                    self._teardown()
                    return
                self._close_sent = True
        # Drain outside the send lock: the drain may answer a ping, which needs
        # that lock. A reader already inside `recv_text` holds the read lock, so
        # wait only briefly and then let the shutdown below wake it.
        if not self._close_received and self._recv_lock.acquire(timeout=_CLOSE_WAIT_S):
            try:
                deadline = time.monotonic() + _CLOSE_WAIT_S
                with contextlib.suppress(WebSocketError, OSError):
                    self._drain_until_close(deadline)
            finally:
                self._recv_lock.release()
        self._teardown()

    @property
    def closed(self) -> bool:
        """Whether the socket has been torn down."""
        return self._closed

    def __enter__(self) -> Self:
        """Return this connection for use as a context manager."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the connection without raising."""
        self.close()

    def _read_message(self, deadline: float | None) -> str:
        """Assemble one text message from the frame stream."""
        fragments: list[bytes] = []
        total = 0
        started = False
        while True:
            budget = self._max_message_bytes - total
            fin, opcode, payload = self._read_frame(deadline, budget)
            if opcode == _OP_PING:
                self._send_control(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CLOSE:
                self._handle_peer_close(payload)
            if opcode == _OP_BINARY:
                raise WebSocketProtocolError(
                    "binary frames are not supported; this transport carries JSON text only"
                )
            if opcode == _OP_TEXT:
                if started:
                    raise WebSocketProtocolError(
                        "a new text frame arrived while a fragmented message was still open"
                    )
                started = True
            elif not started:
                raise WebSocketProtocolError(
                    "a continuation frame arrived with no message to continue"
                )
            fragments.append(payload)
            total += len(payload)
            if fin:
                break
        try:
            return b"".join(fragments).decode("utf-8")
        except UnicodeDecodeError as exc:
            if not self._close_sent:
                self._send_control(_OP_CLOSE, self._close_payload(1007, ""))
                self._close_sent = True
            raise WebSocketProtocolError("text message is not valid UTF-8") from exc

    def _read_frame(self, deadline: float | None, budget: int) -> tuple[bool, int, bytes]:
        """Read one frame, validating its header before allocating for its body.

        Args:
            deadline: Monotonic instant by which the frame must be complete.
            budget: Most payload bytes this frame may carry without pushing the
                message past `max_message_bytes`.

        Returns:
            A `(fin, opcode, payload)` triple with the payload unmasked.

        Raises:
            WebSocketClosed: If the socket ends before the frame is complete.
            WebSocketTimeout: If `deadline` passes mid-frame.
            WebSocketProtocolError: On any header violation.
        """
        if not self._ensure(2, deadline):
            raise WebSocketClosed(1006, "the peer closed the connection")
        first = self._recv_buffer[0]
        second = self._recv_buffer[1]
        fin = bool(first & 0x80)
        if first & 0x70:
            raise WebSocketProtocolError(
                "reserved bits are set; this transport negotiates no extensions"
            )
        opcode = first & 0x0F
        if opcode not in _VALID_OPCODES:
            raise WebSocketProtocolError(f"reserved opcode {opcode:#x}")
        masked = bool(second & 0x80)
        if masked == self._mask_frames:
            sent = "masked" if masked else "unmasked"
            expected = "unmasked" if self._mask_frames else "masked"
            raise WebSocketProtocolError(
                f"frame is {sent} but RFC 6455 requires {expected} frames in this direction"
            )
        length = second & 0x7F
        offset = 2
        if length == 126:
            if not self._ensure(4, deadline):
                raise WebSocketClosed(1006, "the peer closed mid-frame")
            length = int.from_bytes(self._recv_buffer[2:4], "big")
            offset = 4
        elif length == 127:
            if not self._ensure(10, deadline):
                raise WebSocketClosed(1006, "the peer closed mid-frame")
            raw = bytes(self._recv_buffer[2:10])
            if raw[0] & 0x80:
                raise WebSocketProtocolError("the high bit of a 64-bit payload length must be zero")
            length = int.from_bytes(raw, "big")
            offset = 10
        if opcode in _CONTROL_OPCODES:
            if not fin:
                raise WebSocketProtocolError("control frames must not be fragmented")
            if length > _MAX_CONTROL_PAYLOAD:
                raise WebSocketProtocolError(
                    f"control frame payload of {length} bytes exceeds the 125-byte limit"
                )
        elif length > budget:
            raise WebSocketProtocolError(
                f"frame declares {length} payload bytes, over the {budget}-byte remaining limit"
            )
        mask = b""
        if masked:
            if not self._ensure(offset + 4, deadline):
                raise WebSocketClosed(1006, "the peer closed mid-frame")
            mask = bytes(self._recv_buffer[offset : offset + 4])
            offset += 4
        total = offset + length
        if not self._ensure(total, deadline):
            raise WebSocketClosed(1006, "the peer closed mid-frame")
        payload = bytes(self._recv_buffer[offset:total])
        del self._recv_buffer[:total]
        if masked:
            payload = _apply_mask(payload, mask)
        return fin, opcode, payload

    def _ensure(self, count: int, deadline: float | None) -> bool:
        """Buffer at least `count` bytes, honouring the deadline.

        Bytes already buffered survive a deadline miss, so a resumed call reads
        the same frame from the start.

        Waiting is done with `select` instead of `socket.settimeout` so a
        concurrent sender never perturbs this reader's deadline.

        Returns:
            True when enough bytes are buffered, False when the peer's socket
            ended first.

        Raises:
            WebSocketTimeout: If the deadline passes first.
            WebSocketClosed: If the socket read fails or the socket is gone.
        """
        while len(self._recv_buffer) < count:
            self._await_readable(deadline)
            try:
                chunk = self._sock.recv(_RECV_CHUNK)
            except TimeoutError:
                raise WebSocketTimeout("no complete message arrived within the timeout") from None
            except OSError as exc:
                raise WebSocketClosed(1006, f"socket read failed: {exc}") from exc
            if not chunk:
                return False
            self._recv_buffer.extend(chunk)
        return True

    def _await_readable(self, deadline: float | None) -> None:
        """Block until a read can make progress or `deadline` passes.

        Raises:
            WebSocketTimeout: If the deadline passes with nothing to read.
            WebSocketClosed: If the socket is no longer selectable or readable.
        """
        pending = getattr(self._sock, "pending", None)
        if callable(pending) and pending():
            # A TLS socket can hold decrypted bytes that the underlying fd has
            # no record for; selecting first would block on data already here.
            return
        remaining: float | None = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebSocketTimeout("no complete message arrived within the timeout")
        try:
            readable, _, _ = select.select([self._sock], [], [], remaining)
        except (OSError, ValueError) as exc:
            raise WebSocketClosed(1006, f"socket is not selectable: {exc}") from exc
        if not readable:
            raise WebSocketTimeout("no complete message arrived within the timeout")

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        """Write one final frame. The caller holds the send lock."""
        header = bytearray()
        header.append(0x80 | opcode)
        mask_bit = 0x80 if self._mask_frames else 0
        length = len(payload)
        if length < 126:
            header.append(mask_bit | length)
        elif length <= 0xFFFF:
            header.append(mask_bit | 126)
            header.extend(length.to_bytes(2, "big"))
        else:
            header.append(mask_bit | 127)
            header.extend(length.to_bytes(8, "big"))
        body = payload
        if self._mask_frames:
            mask = os.urandom(4)
            header.extend(mask)
            body = _apply_mask(payload, mask)
        self._sock.sendall(bytes(header))
        if body:
            self._sock.sendall(body)

    def _close_payload(self, code: int, reason: str) -> bytes:
        """Build a close frame body, validating the code and reason length.

        Raises:
            WebSocketError: If `code` is out of range or the reason is too long
                to fit a control frame.
        """
        if not 1000 <= code <= 4999:
            raise WebSocketError(f"close code {code} is outside the 1000..4999 range")
        encoded = reason.encode("utf-8")
        if len(encoded) > _MAX_CONTROL_PAYLOAD - 2:
            raise WebSocketError(
                f"close reason of {len(encoded)} bytes exceeds the 123-byte control frame limit"
            )
        return code.to_bytes(2, "big") + encoded

    def _send_close(self, code: int, reason: str) -> None:
        """Send a close frame. The caller holds the send lock.

        Raises:
            WebSocketError: If the code is out of range or the reason is too long
                to fit a control frame.
        """
        self._send_frame(_OP_CLOSE, self._close_payload(code, reason))

    def _send_control(self, opcode: int, payload: bytes) -> None:
        """Write a control frame, taking the send lock.

        Called from the reader thread to answer a ping or echo a close, so it
        must never wait on the read lock. A control reply that cannot be written
        is not surfaced: the next read reports the socket failure, and turning a
        best-effort pong into an exception would mask the real error.
        """
        with self._send_lock:
            if self._closed:
                return
            with contextlib.suppress(OSError):
                self._send_frame(opcode, payload)

    def _handle_peer_close(self, payload: bytes) -> NoReturn:
        """Echo a peer's close once, tear the socket down, and report it.

        Never returns: the caller sees `WebSocketClosed` so it cannot continue
        treating a closed connection as live.
        """
        code, reason = _parse_close_frame(payload)
        self._close_received = True
        if not self._close_sent:
            echo = code if code not in _RESERVED_CLOSE_CODES else 1000
            self._send_control(_OP_CLOSE, self._close_payload(echo, ""))
            self._close_sent = True
        self._teardown()
        raise WebSocketClosed(code, reason)

    def _drain_until_close(self, deadline: float) -> None:
        """Read frames until the peer's close arrives or `deadline` passes."""
        while not self._close_received:
            _, opcode, payload = self._read_frame(deadline, self._max_message_bytes)
            if opcode == _OP_CLOSE:
                self._close_received = True
            elif opcode == _OP_PING:
                self._send_control(_OP_PONG, payload)

    def _teardown(self) -> None:
        """Drop the socket exactly once, ignoring a socket that is already gone.

        The socket is shut down before it is closed so a thread blocked in
        `recv` wakes up immediately instead of waiting for its own deadline. That
        is what stops one stuck reader from holding a shutdown open.
        """
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self._sock.close()


@dataclass
class ServerConnection(_Connection):
    """A WebSocket connection the server accepted from a contribution node.

    Built by `handshake_server` on a socket that has already completed the
    upgrade, so the handshake itself never appears here. Frames this side sends
    are unmasked and frames it accepts must be masked, as RFC 6455 §5.1 requires.

    Limitations are those of the module: text only, no subprotocols, no
    extensions, and the returned connection is valid only while the handler that
    produced it is still running, because the HTTP server framework closes the
    socket when the handler returns.
    """


@dataclass
class ClientConnection(_Connection):
    """A WebSocket connection dialled out to a server over ``ws://`` or ``wss://``.

    Frames this side sends are masked with a fresh random key per frame, and
    frames it accepts must be unmasked. The same limitations as
    `ServerConnection` apply: text only, no subprotocols, no extensions.
    """

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_HANDSHAKE_TIMEOUT_S,
        ssl_context: ssl.SSLContext | None = None,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    ) -> Self:
        """Dial `url` and complete the client handshake.

        Args:
            url: A ``ws://`` or ``wss://`` URL; the path and query are sent as the
                request target.
            headers: Extra handshake headers, such as ``Authorization``. Headers
                the handshake owns (``Host``, ``Upgrade``, ``Connection``,
                ``Sec-WebSocket-*``) may not be overridden.
            timeout: Seconds allowed for the TCP connect, TLS handshake, and the
                server's 101 response.
            ssl_context: TLS context for ``wss://``. Defaults to
                `ssl.create_default_context`, which verifies the certificate and
                hostname. Passing it for a ``ws://`` URL is an error.
            max_message_bytes: Largest reassembled message this side accepts.

        Returns:
            The connected client.

        Raises:
            ValueError: If the URL scheme, host, extra headers, or `ssl_context`
                are inconsistent with a WebSocket upgrade.
            WebSocketError: If the TCP or TLS connection cannot be established.
            WebSocketProtocolError: If the server's response is not a valid,
                verified ``101`` upgrade.
        """
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("ws", "wss"):
            raise ValueError(f"url scheme must be ws or wss, got {parts.scheme!r}")
        host = parts.hostname
        if not host:
            raise ValueError(f"url has no host: {url!r}")
        secure = parts.scheme == "wss"
        if ssl_context is not None and not secure:
            raise ValueError("ssl_context is only meaningful for wss:// urls")
        port = parts.port or (443 if secure else 80)
        resource = parts.path or "/"
        if parts.query:
            resource = f"{resource}?{parts.query}"
        proxy = resolve_proxy(parts.scheme, host)
        if proxy is not None:
            sock: socket.socket = connect_via(proxy, host, port, timeout)
        else:
            sock = socket.create_connection((host, port), timeout=timeout)
        try:
            if secure:
                context = ssl_context if ssl_context is not None else client_ssl_context()
                sock = context.wrap_socket(sock, server_hostname=host)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            sock.sendall(_client_request(host, port, resource, key, headers))
            status, response_headers, remainder = _read_http_head(sock, timeout)
            _verify_server_handshake(status, response_headers, key)
        except BaseException:
            sock.close()
            raise
        sock.settimeout(None)
        return cls(
            peer=f"{host}:{port}",
            _sock=sock,
            _mask_frames=True,
            _max_message_bytes=max_message_bytes,
            _recv_buffer=bytearray(remainder),
        )


def handshake_server(
    handler: http.server.BaseHTTPRequestHandler,
    *,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> ServerConnection:
    """Complete the upgrade on a `BaseHTTPRequestHandler`'s accepted socket.

    Validates the request against RFC 6455 and writes the ``101`` response, then
    hands back a connection that reads from the raw socket. The caller must serve
    the connection before the handler returns, because the server framework
    closes the socket afterwards.

    Every request this transport cannot honour is refused rather than answered
    with a ``101`` the peer would misread: a non-GET method, an HTTP/1.0 request,
    a missing or malformed upgrade, a bad ``Sec-WebSocket-Key``, a version other
    than 13, and any requested subprotocol or extension.

    Args:
        handler: The handler whose request headers have already been parsed.
        max_message_bytes: Largest reassembled message this side will accept.

    Returns:
        The upgraded server-side connection.

    Raises:
        HandshakeRefusal: Carrying the HTTP status and reason the caller must
            write back to the client.
    """
    if handler.command != "GET":
        raise HandshakeRefusal(405, f"{handler.command} is not allowed; the upgrade must use GET")
    if handler.request_version != "HTTP/1.1":
        raise HandshakeRefusal(
            400, f"{handler.request_version} is not supported; the upgrade must use HTTP/1.1"
        )
    headers = handler.headers
    if (headers.get("Upgrade") or "").strip().lower() != "websocket":
        raise HandshakeRefusal(400, "the request must carry 'Upgrade: websocket'")
    if "upgrade" not in _tokens(headers.get("Connection")):
        raise HandshakeRefusal(400, "the Connection header must contain the 'upgrade' token")
    key = (headers.get("Sec-WebSocket-Key") or "").strip()
    if not _is_valid_client_key(key):
        raise HandshakeRefusal(400, "Sec-WebSocket-Key must be 16 bytes, base64-encoded")
    version = (headers.get("Sec-WebSocket-Version") or "").strip()
    if version != "13":
        raise HandshakeRefusal(
            426, f"Sec-WebSocket-Version {version or '(missing)'} is not supported; use 13"
        )
    if headers.get("Sec-WebSocket-Protocol"):
        raise HandshakeRefusal(501, "subprotocols are not supported")
    if headers.get("Sec-WebSocket-Extensions"):
        raise HandshakeRefusal(501, "extensions are not supported")
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {_accept_token(key)}\r\n"
        "\r\n"
    ).encode("ascii")
    handler.connection.sendall(response)
    handler.close_connection = True
    read_ahead = _drain_read_ahead(handler.connection, cast("io.BufferedReader", handler.rfile))
    host, port = handler.client_address
    return ServerConnection(
        peer=f"{host}:{port}",
        _sock=handler.connection,
        _mask_frames=False,
        _max_message_bytes=max_message_bytes,
        _recv_buffer=bytearray(read_ahead),
    )


def _client_request(
    host: str,
    port: int,
    resource: str,
    key: str,
    extra: Mapping[str, str] | None,
) -> bytes:
    """Render the client's upgrade request.

    Raises:
        ValueError: If an extra header would override one the handshake owns, or
            contains a line break that would inject another header.
    """
    lines = [
        f"GET {resource} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if extra:
        for name, value in extra.items():
            if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
                raise ValueError(f"header {name!r} contains a line break")
            if name.strip().lower() in _RESERVED_REQUEST_HEADERS:
                raise ValueError(f"the handshake sets {name!r}; it cannot be overridden")
            lines.append(f"{name}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def _read_http_head(sock: socket.socket, timeout: float) -> tuple[int, dict[str, str], bytes]:
    """Read an HTTP response head, returning its status, headers, and any body bytes.

    Any bytes past the head are returned separately so the framing layer starts
    at the first frame byte rather than in the middle of a buffered read.
    """
    buffer = bytearray()
    deadline = time.monotonic() + timeout
    while b"\r\n\r\n" not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebSocketError("the server did not answer the upgrade within the timeout")
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(_RECV_CHUNK)
        except TimeoutError:
            message = "the server did not answer the upgrade within the timeout"
            raise WebSocketError(message) from None
        except OSError as exc:
            raise WebSocketError(f"the upgrade request failed: {exc}") from exc
        if not chunk:
            raise WebSocketError("the server closed the connection during the upgrade")
        buffer.extend(chunk)
    head, _, remainder = bytes(buffer).partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    fields = lines[0].split(" ", 2)
    if len(fields) < 2 or not fields[1].isdigit():
        raise WebSocketProtocolError(f"malformed status line {lines[0]!r}")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator:
            raise WebSocketProtocolError(f"malformed response header {line!r}")
        headers[name.strip().lower()] = value.strip()
    return int(fields[1]), headers, remainder


def _verify_server_handshake(status: int, headers: Mapping[str, str], key: str) -> None:
    """Refuse a server response that is not a valid, verified 101 upgrade.

    Raises:
        WebSocketProtocolError: If the status is not 101, the response lacks a
            valid upgrade, advertises a version other than 13, carries a wrong
            ``Sec-WebSocket-Accept``, or selects a subprotocol or extension that
            this transport never offered.
    """
    if status != 101:
        raise WebSocketProtocolError(f"the server refused the upgrade with status {status}")
    if (headers.get("upgrade") or "").strip().lower() != "websocket":
        raise WebSocketProtocolError("the 101 response is missing 'Upgrade: websocket'")
    if "upgrade" not in _tokens(headers.get("connection")):
        raise WebSocketProtocolError("the 101 response Connection header lacks the 'upgrade' token")
    version = headers.get("sec-websocket-version")
    if version is not None and version.strip() != "13":
        raise WebSocketProtocolError(f"the server answered for Sec-WebSocket-Version {version!r}")
    accept = (headers.get("sec-websocket-accept") or "").strip()
    if accept != _accept_token(key):
        raise WebSocketProtocolError("the Sec-WebSocket-Accept value does not match the key sent")
    if headers.get("sec-websocket-protocol"):
        raise WebSocketProtocolError("the server selected a subprotocol, but none was offered")
    if headers.get("sec-websocket-extensions"):
        raise WebSocketProtocolError("the server selected an extension, but none was offered")


def _accept_token(key: str) -> str:
    """Return the ``Sec-WebSocket-Accept`` value for a client key.

    SHA-1 is fixed by RFC 6455 §4.2.2 for this value; it is a handshake
    checksum, not a security primitive.
    """
    digest = hashlib.sha1(key.encode("ascii") + GUID.encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _is_valid_client_key(key: str) -> bool:
    """Whether `key` is base64 for exactly 16 bytes, as RFC 6455 §4.1 requires."""
    if not key:
        return False
    try:
        decoded = base64.b64decode(key, validate=True)
    except ValueError:
        return False
    return len(decoded) == 16


def _tokens(value: str | None) -> frozenset[str]:
    """Split a comma-separated header value into lowercase tokens."""
    if not value:
        return frozenset()
    return frozenset(token.strip().lower() for token in value.split(","))


def _apply_mask(payload: bytes, mask: bytes) -> bytes:
    """Apply RFC 6455 §5.3 masking; the operation is its own inverse."""
    if not payload:
        return payload
    repeated = (mask * (len(payload) // 4 + 1))[: len(payload)]
    return bytes(byte ^ key for byte, key in zip(payload, repeated, strict=True))


def _parse_close_frame(payload: bytes) -> tuple[int, str]:
    """Decode a close frame body into its code and reason.

    Raises:
        WebSocketProtocolError: If the payload is one byte, the code is one that
            may not appear on the wire, or the reason is not valid UTF-8.
    """
    if not payload:
        return 1005, ""
    if len(payload) == 1:
        raise WebSocketProtocolError("close frame body is 1 byte; a close code needs 2")
    code = int.from_bytes(payload[:2], "big")
    if code < 1000 or code in _RESERVED_CLOSE_CODES or code > 4999:
        raise WebSocketProtocolError(f"close code {code} may not appear on the wire")
    try:
        reason = payload[2:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebSocketProtocolError("close reason is not valid UTF-8") from exc
    return code, reason


def _drain_read_ahead(sock: socket.socket, reader: io.BufferedReader) -> bytes:
    """Return bytes the HTTP layer read past the request headers.

    `BaseHTTPRequestHandler` parses the request through a buffered reader, which
    can pull the first frame bytes off the socket alongside the headers. Draining
    it hands those bytes to the framing layer so the stream is read from its
    first byte.

    On a non-blocking socket an empty read surfaces as `BlockingIOError` on a
    plain socket and as `ssl.SSLWantReadError` under TLS; both mean "nothing more
    is buffered right now".
    """
    collected = bytearray()
    sock.setblocking(False)
    try:
        while True:
            try:
                chunk = reader.read1(_RECV_CHUNK)
            except (BlockingIOError, InterruptedError, ssl.SSLWantReadError):
                break
            if not chunk:
                break
            collected.extend(chunk)
    finally:
        sock.setblocking(True)
    return bytes(collected)
