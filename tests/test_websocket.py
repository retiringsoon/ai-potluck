"""Tests for the RFC 6455 WebSocket transport.

Every test runs over a real TCP loopback socket pair. The server side is a real
`http.server.ThreadingHTTPServer` whose handler calls `handshake_server`, and the
client side is either the module's own `ClientConnection` or a raw socket that
writes frames by hand. The socket layer is never mocked, because the framing,
masking, and buffering behaviour is exactly what is under test.

The raw-socket helpers below build and parse frames independently of the module
under test, so a framing bug cannot hide behind a shared helper.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import http.server
import os
import socket
import ssl
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from aipotluck.websocket import (
    GUID,
    ClientConnection,
    HandshakeRefusal,
    ServerConnection,
    WebSocketClosed,
    WebSocketError,
    WebSocketProtocolError,
    WebSocketTimeout,
    handshake_server,
)

#: A valid ``Sec-WebSocket-Key``: base64 for exactly 16 bytes.
VALID_KEY = base64.b64encode(b"0123456789abcdef").decode("ascii")

OP_CONT = 0x0
OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


# --------------------------------------------------------------------------- #
# Raw frame helpers: client -> server frames must be masked (RFC 6455 §5.1).
# --------------------------------------------------------------------------- #


def _apply_mask(payload: bytes, mask: bytes) -> bytes:
    """Mask or unmask `payload`; the operation is its own inverse."""
    return bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))


def _client_frame(
    opcode: int,
    payload: bytes = b"",
    *,
    fin: bool = True,
    masked: bool = True,
    mask: bytes = b"\x0a\x0b\x0c\x0d",
) -> bytes:
    """Build a frame as a client would send it, including a mask when asked."""
    header = bytearray([(0x80 if fin else 0x00) | opcode])
    length = len(payload)
    mask_bit = 0x80 if masked else 0x00
    if length > 0xFFFF:
        header.append(mask_bit | 127)
        header.extend(length.to_bytes(8, "big"))
    elif length > 125:
        header.append(mask_bit | 126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(mask_bit | length)
    if masked:
        header.extend(mask)
        payload = _apply_mask(payload, mask)
    return bytes(header) + payload


def _read_exactly(source: socket.socket, count: int) -> bytes:
    """Read exactly `count` bytes from `source` or fail the test."""
    buffer = bytearray()
    while len(buffer) < count:
        chunk = source.recv(count - len(buffer))
        assert chunk, "the peer closed before the frame was complete"
        buffer.extend(chunk)
    return bytes(buffer)


def _read_server_frame(source: socket.socket) -> tuple[bool, int, bytes]:
    """Read one frame as a server would send it; server frames must be unmasked."""
    first, second = _read_exactly(source, 2)
    assert not second & 0x80, "a server frame must not be masked"
    opcode = first & 0x0F
    fin = bool(first & 0x80)
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(_read_exactly(source, 2), "big")
    elif length == 127:
        length = int.from_bytes(_read_exactly(source, 8), "big")
    return fin, opcode, _read_exactly(source, length) if length else b""


def _read_head(source: socket.socket) -> str:
    """Read an HTTP head, up to and including the blank line."""
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = source.recv(4096)
        if not chunk:
            break
        buffer.extend(chunk)
    return bytes(buffer).decode("latin-1")


def _status_of(head: str) -> int:
    """Return the status code from an HTTP status line."""
    return int(head.split(" ", 2)[1])


def _raw_client(host: str, port: int, *, path: str = "/share", extra: str = "") -> socket.socket:
    """Dial the server and complete a valid upgrade, returning the raw socket."""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"{extra}"
        "\r\n"
    ).encode("ascii")
    source = socket.create_connection((host, port), timeout=5.0)
    source.sendall(request)
    head = _read_head(source)
    assert head.startswith("HTTP/1.1 101"), head
    return source


def _upgrade_request(
    *,
    method: str = "GET",
    version: str = "HTTP/1.1",
    key: str | None = VALID_KEY,
    ws_version: str | None = "13",
    upgrade: str | None = "websocket",
    connection: str | None = "Upgrade",
    extra: tuple[tuple[str, str], ...] = (),
) -> bytes:
    """Build a raw upgrade request, omitting or overriding headers on request."""
    lines = [f"{method} /share {version}", "Host: 127.0.0.1"]
    if upgrade is not None:
        lines.append(f"Upgrade: {upgrade}")
    if connection is not None:
        lines.append(f"Connection: {connection}")
    if key is not None:
        lines.append(f"Sec-WebSocket-Key: {key}")
    if ws_version is not None:
        lines.append(f"Sec-WebSocket-Version: {ws_version}")
    lines.extend(f"{name}: {value}" for name, value in extra)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


# --------------------------------------------------------------------------- #
# Server harness
# --------------------------------------------------------------------------- #

Callback = Callable[[ServerConnection, "Harness"], None]


class _TLSServer(http.server.ThreadingHTTPServer):
    """A `ThreadingHTTPServer` that wraps each accepted socket in TLS."""

    def __init__(
        self, address: tuple[str, int], handler: type, context: ssl.SSLContext
    ) -> None:
        self._context = context
        super().__init__(address, handler)

    def get_request(self) -> tuple[socket.socket, object]:
        """Accept a socket and complete the TLS handshake before the handler runs."""
        connection, address = super().get_request()
        return self._context.wrap_socket(connection, server_side=True), address


class Harness:
    """Serves one upgrade and one session per accepted connection, in the background."""

    def __init__(
        self,
        callback: Callback | None = None,
        *,
        max_message_bytes: int = 256 * 1024,
        tls: ssl.SSLContext | None = None,
    ) -> None:
        self.callback = callback
        self.max_message_bytes = max_message_bytes
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.refusal: HandshakeRefusal | None = None
        self.request_headers: dict[str, str] = {}
        self.connection_peer: str | None = None
        self.observed: dict[str, object] = {}
        handler = self._build_handler()
        if tls is None:
            self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        else:
            self.server = _TLSServer(("127.0.0.1", 0), handler, tls)
        self.thread = threading.Thread(target=self._serve_forever, daemon=True)

    def _serve_forever(self) -> None:
        self.server.serve_forever(poll_interval=0.05)

    def _build_handler(self) -> type:
        harness = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                """Silence the default stderr access log."""

            def do_GET(self) -> None:
                harness.serve(self)

            do_POST = do_GET

        return Handler

    @property
    def host(self) -> str:
        """The loopback address the server is bound to."""
        return "127.0.0.1"

    @property
    def port(self) -> int:
        """The ephemeral port the server is bound to."""
        return int(self.server.server_address[1])

    def start(self) -> None:
        """Start accepting connections."""
        self.thread.start()

    def stop(self) -> None:
        """Stop the server and join its thread."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)

    def serve(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        """Run the upgrade and then the per-test callback, on the handler's thread."""
        self.request_headers = {name.lower(): value for name, value in handler.headers.items()}
        try:
            connection = handshake_server(handler, max_message_bytes=self.max_message_bytes)
        except HandshakeRefusal as refusal:
            self.refusal = refusal
            body = f"{refusal.status} {refusal.reason}\n".encode()
            handler.send_response(refusal.status)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            handler.close_connection = True
            self.done.set()
            return
        self.connection_peer = connection.peer
        try:
            if self.callback is not None:
                self.callback(connection, self)
        except Exception as exc:
            self.error = exc
        finally:
            connection.close()
            self.done.set()


def _wait(harness: Harness) -> None:
    """Block until the handler finished, failing the test if it never does."""
    assert harness.done.wait(10.0), "the server handler never finished"


def _finish(harness: Harness) -> None:
    """Wait for the handler and re-raise whatever it raised."""
    _wait(harness)
    if harness.error is not None:
        raise harness.error


@pytest.fixture
def serve() -> Iterator[Callable[..., Harness]]:
    """Return a factory that starts a loopback upgrade server per call."""
    harnesses: list[Harness] = []

    def start(
        callback: Callback | None = None,
        *,
        max_message_bytes: int = 256 * 1024,
        tls: ssl.SSLContext | None = None,
    ) -> Harness:
        harness = Harness(callback, max_message_bytes=max_message_bytes, tls=tls)
        harness.start()
        harnesses.append(harness)
        return harness

    yield start
    for harness in harnesses:
        harness.stop()


# --------------------------------------------------------------------------- #
# 1. Handshake and a text round trip in both directions
# --------------------------------------------------------------------------- #


def test_client_and_server_exchange_text_in_both_directions(serve) -> None:
    def callback(connection: ServerConnection, harness: Harness) -> None:
        first = connection.recv_text(timeout=5.0)
        connection.send_text(f"echo:{first}")
        second = connection.recv_text(timeout=5.0)
        connection.send_text(f"again:{second}")

    harness = serve(callback)
    url = f"ws://{harness.host}:{harness.port}/share"
    client = ClientConnection.connect(url)
    assert client.closed is False
    assert client.peer == f"{harness.host}:{harness.port}"
    with client:
        client.send_text("hello")
        assert client.recv_text(timeout=5.0) == "echo:hello"
        client.send_text("world")
        assert client.recv_text(timeout=5.0) == "again:world"
    assert client.closed is True
    _finish(harness)
    assert harness.connection_peer is not None
    assert harness.connection_peer.startswith("127.0.0.1:")
    assert harness.connection_peer != client.peer


def test_wss_round_trip_over_tls(serve) -> None:
    # A private scratch directory rather than pytest's shared tmp factory: the
    # certificate files are two entries, so cleaning them up cannot trip a
    # bulk-delete guard, and the test does not depend on the basetemp directory
    # other suites share.
    with tempfile.TemporaryDirectory() as directory:
        certificate, key = _write_self_signed_certificate(Path(directory))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certificate, key)

        def callback(connection: ServerConnection, harness: Harness) -> None:
            connection.send_text("secure:" + connection.recv_text(timeout=5.0))

        harness = serve(callback, tls=server_context)
        client_context = ssl.create_default_context(cafile=certificate)
        url = f"wss://localhost:{harness.port}/share"
        with ClientConnection.connect(url, ssl_context=client_context) as client:
            client.send_text("hi")
            assert client.recv_text(timeout=5.0) == "secure:hi"
        _finish(harness)


def _write_self_signed_certificate(directory: Path) -> tuple[str, str]:
    """Write a self-signed certificate and key for ``localhost`` into `directory`."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / "certificate.pem"
    key_path = directory / "key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(certificate_path), str(key_path)


# --------------------------------------------------------------------------- #
# 2. Extra handshake headers
# --------------------------------------------------------------------------- #


def test_client_authorization_header_reaches_the_handler(serve) -> None:
    def callback(connection: ServerConnection, harness: Harness) -> None:
        connection.send_text(harness.request_headers.get("authorization", "<missing>"))

    harness = serve(callback)
    with ClientConnection.connect(
        f"ws://{harness.host}:{harness.port}/share",
        headers={"Authorization": "Bearer sekrit-token"},
    ) as client:
        assert client.recv_text(timeout=5.0) == "Bearer sekrit-token"
    _finish(harness)
    assert harness.request_headers["authorization"] == "Bearer sekrit-token"


def test_connect_refuses_to_override_handshake_headers(serve) -> None:
    harness = serve()
    url = f"ws://{harness.host}:{harness.port}/share"
    with pytest.raises(ValueError, match="cannot be overridden"):
        ClientConnection.connect(url, headers={"Sec-WebSocket-Key": "not-our-key"})


def test_connect_refuses_a_non_websocket_scheme(serve) -> None:
    harness = serve()
    with pytest.raises(ValueError, match="scheme must be ws or wss"):
        ClientConnection.connect(f"http://{harness.host}:{harness.port}/share")


def _read_client_frame(source: socket.socket) -> tuple[int, bytes, bytes]:
    """Read one frame as a client sends it; the mask is expected and removed."""
    first, second = _read_exactly(source, 2)
    assert second & 0x80, "a client frame must be masked"
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(_read_exactly(source, 2), "big")
    elif length == 127:
        length = int.from_bytes(_read_exactly(source, 8), "big")
    mask = _read_exactly(source, 4)
    payload = _apply_mask(_read_exactly(source, length), mask) if length else b""
    return opcode, mask, payload


def _header_value(head: str, name: str) -> str:
    """Return a header value from a decoded HTTP head, case-insensitively."""
    for line in head.split("\r\n")[1:]:
        field, separator, value = line.partition(":")
        if separator and field.strip().lower() == name:
            return value.strip()
    raise AssertionError(f"{name} is not in the response head")


def test_client_masks_every_frame_with_a_fresh_random_key() -> None:
    captured: list[tuple[int, bytes, bytes]] = []
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])

    def run() -> None:
        connection, _ = listener.accept()
        try:
            head = _read_head(connection)
            key = _header_value(head, "sec-websocket-key")
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest())
            connection.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept.decode('ascii')}\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            captured.append(_read_client_frame(connection))
            captured.append(_read_client_frame(connection))
        finally:
            connection.close()
            listener.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        with ClientConnection.connect(f"ws://127.0.0.1:{port}/share") as client:
            client.send_text("alpha")
            client.send_text("b" * 300)
    finally:
        thread.join(timeout=5.0)

    opcodes = [opcode for opcode, _, _ in captured]
    payloads = [payload for _, _, payload in captured]
    masks = [mask for _, mask, _ in captured]
    assert opcodes == [OP_TEXT, OP_TEXT]
    assert payloads == [b"alpha", b"b" * 300]
    assert all(len(mask) == 4 for mask in masks)
    assert masks[0] != masks[1]


def test_upgrade_header_names_are_case_insensitive(serve) -> None:
    def callback(connection: ServerConnection, harness: Harness) -> None:
        connection.send_text("echo:" + connection.recv_text(timeout=5.0))

    harness = serve(callback)
    source = socket.create_connection((harness.host, harness.port), timeout=5.0)
    try:
        source.sendall(
            b"GET /share HTTP/1.1\r\n"
            b"host: 127.0.0.1\r\n"
            b"upgrade: WebSocket\r\n"
            b"CONNECTION: keep-alive, UpGrAdE\r\n"
            b"sec-websocket-key: " + VALID_KEY.encode("ascii") + b"\r\n"
            b"SEC-WEBSOCKET-VERSION: 13\r\n"
            b"\r\n"
        )
        head = _read_head(source)
        assert head.startswith("HTTP/1.1 101"), head
        source.sendall(_client_frame(OP_TEXT, b"probe"))
        fin, opcode, body = _read_server_frame(source)
    finally:
        source.close()
    assert (fin, opcode, body) == (True, OP_TEXT, b"echo:probe")
    _finish(harness)


# --------------------------------------------------------------------------- #
# 3. The 64-bit payload length path
# --------------------------------------------------------------------------- #


def test_large_message_round_trips_through_the_64_bit_length_path(serve) -> None:
    payload = "x" * 100_000
    assert len(payload.encode("utf-8")) > 0xFFFF

    def callback(connection: ServerConnection, harness: Harness) -> None:
        connection.send_text(connection.recv_text(timeout=10.0))

    harness = serve(callback)
    with ClientConnection.connect(f"ws://{harness.host}:{harness.port}/share") as client:
        client.send_text(payload)
        echoed = client.recv_text(timeout=10.0)
    assert echoed == payload
    assert len(echoed) == 100_000
    _finish(harness)


# --------------------------------------------------------------------------- #
# 4. Fragmentation
# --------------------------------------------------------------------------- #


def test_fragmented_message_is_reassembled(serve) -> None:
    def callback(connection: ServerConnection, harness: Harness) -> None:
        connection.send_text(connection.recv_text(timeout=5.0))

    harness = serve(callback)
    source = _raw_client(harness.host, harness.port)
    try:
        source.sendall(_client_frame(OP_TEXT, b"Hel", fin=False))
        source.sendall(_client_frame(OP_CONT, b"lo, ", fin=False))
        source.sendall(_client_frame(OP_CONT, b"world", fin=True))
        fin, opcode, body = _read_server_frame(source)
    finally:
        source.close()
    assert (fin, opcode, body) == (True, OP_TEXT, b"Hello, world")
    _finish(harness)


def test_fragmented_message_over_the_limit_is_refused_before_buffering(serve) -> None:
    harness = serve(lambda connection, _: connection.recv_text(timeout=5.0), max_message_bytes=1000)
    source = _raw_client(harness.host, harness.port)
    try:
        source.sendall(_client_frame(OP_TEXT, b"a" * 600, fin=False))
        source.sendall(_client_frame(OP_CONT, b"b" * 600, fin=True))
        _wait(harness)
    finally:
        source.close()
    assert isinstance(harness.error, WebSocketProtocolError), harness.error
    assert "over the 400-byte remaining limit" in str(harness.error)


# --------------------------------------------------------------------------- #
# 5. Timeouts
# --------------------------------------------------------------------------- #


def test_recv_text_times_out_then_delivers_a_late_message(serve) -> None:
    def callback(connection: ServerConnection, harness: Harness) -> None:
        try:
            connection.recv_text(timeout=0.3)
        except WebSocketTimeout:
            harness.observed["timed_out"] = True
        harness.observed["message"] = connection.recv_text(timeout=5.0)
        connection.send_text(f"got:{harness.observed['message']}")

    harness = serve(callback)
    with ClientConnection.connect(f"ws://{harness.host}:{harness.port}/share") as client:
        time.sleep(0.6)
        client.send_text("late")
        assert client.recv_text(timeout=5.0) == "got:late"
    _finish(harness)
    assert harness.observed["timed_out"] is True
    assert harness.observed["message"] == "late"


def test_recv_text_raises_timeout_with_a_short_deadline(serve) -> None:
    def callback(connection: ServerConnection, harness: Harness) -> None:
        started = time.monotonic()
        try:
            connection.recv_text(timeout=0.2)
        except WebSocketTimeout as exc:
            harness.observed["elapsed"] = time.monotonic() - started
            harness.observed["detail"] = str(exc)

    harness = serve(callback)
    with ClientConnection.connect(f"ws://{harness.host}:{harness.port}/share"):
        _wait(harness)
    assert 0.15 <= float(harness.observed["elapsed"]) < 3.0
    assert "within the timeout" in str(harness.observed["detail"])


# --------------------------------------------------------------------------- #
# 6. Control frames are transparent
# --------------------------------------------------------------------------- #


def test_ping_is_answered_and_pong_is_dropped_without_surfacing(serve) -> None:
    def callback(connection: ServerConnection, harness: Harness) -> None:
        harness.observed["message"] = connection.recv_text(timeout=5.0)

    harness = serve(callback)
    source = _raw_client(harness.host, harness.port)
    try:
        source.sendall(_client_frame(OP_PING, b"ping-body"))
        source.sendall(_client_frame(OP_PONG, b"pong-body"))
        source.sendall(_client_frame(OP_TEXT, b"payload", fin=True))
        fin, opcode, body = _read_server_frame(source)
    finally:
        source.close()
    assert (fin, opcode, body) == (True, OP_PONG, b"ping-body")
    _wait(harness)
    assert harness.error is None
    assert harness.observed["message"] == "payload"


# --------------------------------------------------------------------------- #
# 7. Clean close
# --------------------------------------------------------------------------- #


def test_clean_close_reports_the_peer_code_and_reason(serve) -> None:
    def callback(connection: ServerConnection, harness: Harness) -> None:
        assert connection.recv_text(timeout=5.0) == "bye"
        connection.send_text("closing")
        try:
            connection.recv_text(timeout=5.0)
        except WebSocketClosed as exc:
            harness.observed["code"] = exc.code
            harness.observed["reason"] = exc.reason

    harness = serve(callback)
    client = ClientConnection.connect(f"ws://{harness.host}:{harness.port}/share")
    client.send_text("bye")
    assert client.recv_text(timeout=5.0) == "closing"
    client.close(4001, "done here")
    assert client.closed is True
    _finish(harness)
    assert harness.observed["code"] == 4001
    assert harness.observed["reason"] == "done here"


def test_peer_close_frame_is_echoed_and_surfaces_as_websocket_closed(serve) -> None:
    def callback(connection: ServerConnection, harness: Harness) -> None:
        try:
            connection.recv_text(timeout=5.0)
        except WebSocketClosed as exc:
            harness.observed["code"] = exc.code

    harness = serve(callback)
    source = _raw_client(harness.host, harness.port)
    try:
        source.sendall(_client_frame(OP_CLOSE, (1001).to_bytes(2, "big")))
        fin, opcode, body = _read_server_frame(source)
    finally:
        source.close()
    assert fin is True
    assert opcode == OP_CLOSE
    assert int.from_bytes(body[:2], "big") == 1001
    _wait(harness)
    assert harness.observed["code"] == 1001


# --------------------------------------------------------------------------- #
# 8. Handshake refusals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("case", "request_bytes", "expected_status"),
    [
        ("non-get", _upgrade_request(method="POST"), 405),
        ("http-1-0", _upgrade_request(version="HTTP/1.0"), 400),
        ("no-upgrade-header", _upgrade_request(upgrade=None), 400),
        ("connection-without-upgrade", _upgrade_request(connection="keep-alive"), 400),
        ("short-key", _upgrade_request(key="tooshort"), 400),
        ("non-base64-key", _upgrade_request(key="!" * 24), 400),
        ("wrong-version", _upgrade_request(ws_version="8"), 426),
        ("missing-version", _upgrade_request(ws_version=None), 426),
        ("subprotocol", _upgrade_request(extra=(("Sec-WebSocket-Protocol", "chat"),)), 501),
        (
            "extension",
            _upgrade_request(extra=(("Sec-WebSocket-Extensions", "permessage-deflate"),)),
            501,
        ),
    ],
)
def test_upgrade_refusals_report_the_expected_status(serve, case, request_bytes, expected_status):
    harness = serve()
    source = socket.create_connection((harness.host, harness.port), timeout=5.0)
    try:
        source.sendall(request_bytes)
        status = _status_of(_read_head(source))
    finally:
        source.close()
    _wait(harness)
    assert status == expected_status, case
    assert harness.refusal is not None, case
    assert harness.refusal.status == expected_status, case
    assert harness.refusal.reason, case


# --------------------------------------------------------------------------- #
# 9. Protocol violations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("case", "frame", "needle"),
    [
        ("reserved-opcode", _client_frame(0x3, b"nope"), "reserved opcode 0x3"),
        (
            "unmasked-client-frame",
            _client_frame(OP_TEXT, b"hello", masked=False),
            "requires masked frames in this direction",
        ),
        (
            "oversized-control-frame",
            _client_frame(OP_PING, b"p" * 126),
            "exceeds the 125-byte limit",
        ),
        (
            "fragmented-control-frame",
            _client_frame(OP_PING, b"p", fin=False),
            "control frames must not be fragmented",
        ),
        (
            "reserved-bits",
            bytes([0xC1, 0x80, 0, 0, 0, 0]),
            "reserved bits are set",
        ),
        (
            "negative-64-bit-length",
            bytes([0x81, 0xFF]) + (1 << 63).to_bytes(8, "big"),
            "high bit of a 64-bit payload length",
        ),
    ],
)
def test_framing_violations_are_refused(serve, case, frame, needle) -> None:
    harness = serve(lambda connection, _: connection.recv_text(timeout=5.0))
    source = _raw_client(harness.host, harness.port)
    try:
        source.sendall(frame)
        _wait(harness)
    finally:
        source.close()
    assert isinstance(harness.error, WebSocketProtocolError), (case, harness.error)
    assert needle in str(harness.error), case


def test_invalid_utf8_text_frame_closes_with_code_1007(serve) -> None:
    harness = serve(lambda connection, _: connection.recv_text(timeout=5.0))
    source = _raw_client(harness.host, harness.port)
    try:
        source.sendall(_client_frame(OP_TEXT, b"\xff\xfe\xfd"))
        fin, opcode, body = _read_server_frame(source)
        _wait(harness)
    finally:
        source.close()
    assert isinstance(harness.error, WebSocketProtocolError), harness.error
    assert "not valid UTF-8" in str(harness.error)
    assert fin is True
    assert opcode == OP_CLOSE
    assert int.from_bytes(body[:2], "big") == 1007


def test_binary_frame_is_refused(serve) -> None:
    harness = serve(lambda connection, _: connection.recv_text(timeout=5.0))
    source = _raw_client(harness.host, harness.port)
    try:
        source.sendall(_client_frame(0x2, b"\x00\x01\x02"))
        _wait(harness)
    finally:
        source.close()
    assert isinstance(harness.error, WebSocketProtocolError), harness.error
    assert "binary frames are not supported" in str(harness.error)


def test_interrupted_fragmented_message_is_refused(serve) -> None:
    harness = serve(lambda connection, _: connection.recv_text(timeout=5.0))
    source = _raw_client(harness.host, harness.port)
    try:
        source.sendall(_client_frame(OP_TEXT, b"first", fin=False))
        source.sendall(_client_frame(OP_TEXT, b"second", fin=True))
        _wait(harness)
    finally:
        source.close()
    assert isinstance(harness.error, WebSocketProtocolError), harness.error
    assert "while a fragmented message was still open" in str(harness.error)


def test_oversized_single_frame_is_refused_before_reading_the_payload(serve) -> None:
    harness = serve(lambda connection, _: connection.recv_text(timeout=5.0), max_message_bytes=2000)
    source = _raw_client(harness.host, harness.port)
    try:
        # Declares 8 KiB, over the 2 KiB limit, and sends no payload at all.
        header = bytes([0x81, 0xFE]) + (8192).to_bytes(2, "big")
        source.sendall(header)
        _wait(harness)
    finally:
        source.close()
    assert isinstance(harness.error, WebSocketProtocolError), harness.error
    assert "over the 2000-byte remaining limit" in str(harness.error)


# --------------------------------------------------------------------------- #
# 10. close() is idempotent
# --------------------------------------------------------------------------- #


def test_close_is_idempotent(serve) -> None:
    def callback(connection: ServerConnection, harness: Harness) -> None:
        assert connection.recv_text(timeout=5.0) == "hi"
        try:
            connection.recv_text(timeout=5.0)
        except WebSocketClosed as exc:
            harness.observed["code"] = exc.code

    harness = serve(callback)
    client = ClientConnection.connect(f"ws://{harness.host}:{harness.port}/share")
    client.send_text("hi")
    client.close()
    client.close()
    client.close()
    assert client.closed is True
    with pytest.raises(WebSocketError, match="cannot send on a closed connection"):
        client.send_text("too late")
    _finish(harness)
    assert harness.observed["code"] == 1000


def test_context_manager_closes_without_raising(serve) -> None:
    def callback(connection: ServerConnection, harness: Harness) -> None:
        assert connection.recv_text(timeout=5.0) == "hi"
        try:
            connection.recv_text(timeout=5.0)
        except WebSocketClosed as exc:
            harness.observed["code"] = exc.code

    harness = serve(callback)
    with ClientConnection.connect(f"ws://{harness.host}:{harness.port}/share") as client:
        client.send_text("hi")
        assert client.closed is False
    assert client.closed is True
    _finish(harness)
    assert harness.observed["code"] == 1000


def test_server_close_frame_is_reported_with_its_code(serve) -> None:
    def callback(connection: ServerConnection, harness: Harness) -> None:
        connection.send_text("ready")
        connection.close(4009, "maintenance")

    harness = serve(callback)
    with ClientConnection.connect(f"ws://{harness.host}:{harness.port}/share") as client:
        assert client.recv_text(timeout=5.0) == "ready"
        with pytest.raises(WebSocketClosed) as caught:
            client.recv_text(timeout=5.0)
    assert caught.value.code == 4009
    assert caught.value.reason == "maintenance"
    _finish(harness)


def test_handshake_response_verification_rejects_a_wrong_accept() -> None:
    """A server that computes the wrong Sec-WebSocket-Accept is refused."""
    server, thread = _lying_upgrade_server(accept="AAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    try:
        with pytest.raises(WebSocketProtocolError, match="Sec-WebSocket-Accept"):
            ClientConnection.connect(f"ws://127.0.0.1:{server.server_address[1]}/share")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def test_handshake_response_verification_rejects_a_missing_upgrade() -> None:
    server, thread = _lying_upgrade_server(omit_upgrade=True)
    try:
        with pytest.raises(WebSocketProtocolError, match="missing 'Upgrade: websocket'"):
            ClientConnection.connect(f"ws://127.0.0.1:{server.server_address[1]}/share")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def test_handshake_response_verification_rejects_a_bad_version() -> None:
    server, thread = _lying_upgrade_server(version="8")
    try:
        with pytest.raises(WebSocketProtocolError, match="Sec-WebSocket-Version"):
            ClientConnection.connect(f"ws://127.0.0.1:{server.server_address[1]}/share")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def test_handshake_response_verification_rejects_a_non_101_status() -> None:
    server, thread = _lying_upgrade_server(status="403 Forbidden")
    try:
        with pytest.raises(WebSocketProtocolError, match="status 403"):
            ClientConnection.connect(f"ws://127.0.0.1:{server.server_address[1]}/share")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def _lying_upgrade_server(
    *,
    accept: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    omit_upgrade: bool = False,
    version: str | None = None,
    status: str = "101 Switching Protocols",
) -> tuple[http.server.ThreadingHTTPServer, threading.Thread]:
    """Return a server that answers every request with a hand-written upgrade reply."""
    lines = [f"HTTP/1.1 {status}"]
    if not omit_upgrade:
        lines.append("Upgrade: websocket")
    lines.append("Connection: Upgrade")
    lines.append(f"Sec-WebSocket-Accept: {accept}")
    if version is not None:
        lines.append(f"Sec-WebSocket-Version: {version}")
    reply = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            """Silence the default stderr access log."""

        def do_GET(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.connection.sendall(reply)
            self.close_connection = True

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    return server, thread


# --------------------------------------------------------------------------- #
# 10. Read and write are independent directions
# --------------------------------------------------------------------------- #


def test_send_does_not_wait_for_a_blocked_reader(serve) -> None:
    """A stalled reader must not delay messages this side has to send.

    A reader blocked in `recv_text` holds the read lock for the whole wait. If
    sending shared that lock, one peer that stopped reading would freeze every
    message the other direction owed it — which on this platform means a slow
    output consumer could starve a node's ACK, or a stalled node could starve a
    consumer's stream.
    """
    def callback(connection: ServerConnection, harness: Harness) -> None:
        reader_result: list[object] = []

        def reader() -> None:
            try:
                reader_result.append(connection.recv_text(timeout=10.0))
            except Exception as exc:  # recorded so the assertion can name it
                reader_result.append(exc)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        time.sleep(0.3)
        began = time.monotonic()
        connection.send_text("control-while-reading")
        harness.observed["send_elapsed"] = time.monotonic() - began
        thread.join(timeout=10.0)
        harness.observed["reader_result"] = reader_result
        harness.observed["reader_alive"] = thread.is_alive()

    harness = serve(callback)
    url = f"ws://{harness.host}:{harness.port}/share"
    with ClientConnection.connect(url, timeout=5.0) as client:
        assert client.recv_text(timeout=5.0) == "control-while-reading"
        client.send_text("client-reply")
        _finish(harness)

    elapsed = harness.observed["send_elapsed"]
    assert isinstance(elapsed, float)
    assert elapsed < 0.5, f"send waited {elapsed:.3f}s behind a blocked reader"
    assert harness.observed["reader_alive"] is False, "the reader thread never returned"
    assert harness.observed["reader_result"] == ["client-reply"]


def test_close_wakes_a_reader_blocked_without_a_deadline(serve) -> None:
    """`close` must not hang behind a reader that has no timeout.

    Shutdown paths call this while another thread may be parked in an untimed
    read. The socket is shut down before it is closed precisely so that reader
    wakes immediately, instead of holding the process open until its own
    deadline — which, with no deadline, never arrives.
    """
    def callback(connection: ServerConnection, harness: Harness) -> None:
        wake: dict[str, object] = {}

        def reader() -> None:
            began = time.monotonic()
            try:
                connection.recv_text(timeout=None)
                wake["raised"] = "no-exception"
            except Exception as exc:
                wake["raised"] = type(exc).__name__
            wake["reader_elapsed"] = time.monotonic() - began

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        time.sleep(0.3)
        began = time.monotonic()
        connection.close()
        wake["close_elapsed"] = time.monotonic() - began
        thread.join(timeout=5.0)
        wake["reader_alive"] = thread.is_alive()
        harness.observed["wake"] = wake

    harness = serve(callback)
    with ClientConnection.connect(f"ws://{harness.host}:{harness.port}/share", timeout=5.0):
        _finish(harness)

    wake = harness.observed["wake"]
    assert isinstance(wake, dict)
    assert wake["reader_alive"] is False, "close() did not wake the blocked reader"
    assert wake["raised"] == "WebSocketClosed"
    close_elapsed = wake["close_elapsed"]
    reader_elapsed = wake["reader_elapsed"]
    assert isinstance(close_elapsed, float) and isinstance(reader_elapsed, float)
    assert close_elapsed < 3.0, f"close() blocked for {close_elapsed:.3f}s"
    assert reader_elapsed < 3.0, f"the reader stayed blocked for {reader_elapsed:.3f}s"
