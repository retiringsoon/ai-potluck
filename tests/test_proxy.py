"""Proxy support: URL parsing, environment resolution, and live relay round-trips.

The relay tests use fake in-process proxies that forward to real loopback
servers under a fake hostname (``center.example``), which also proves the
remote-resolution semantics: the client never resolves the destination, the
proxy does.
"""

from __future__ import annotations

import http.server
import select
import socket
import threading

import pytest
from test_websocket import Harness

from aipotluck.proxy import (
    ProxyConnectError,
    connect_via,
    http_connection_for,
    is_loopback_host,
    parse_proxy_url,
    resolve_proxy,
)

FAKE_HOST = "center.example"


# --------------------------------------------------------------------------- #
# Parsing and environment resolution
# --------------------------------------------------------------------------- #


def test_parse_proxy_url_defaults_and_auth() -> None:
    endpoint = parse_proxy_url("socks5://alice:secret@10.0.0.9")
    assert endpoint.scheme == "socks5"
    assert (endpoint.host, endpoint.port) == ("10.0.0.9", 1080)
    assert (endpoint.username, endpoint.password) == ("alice", "secret")
    assert parse_proxy_url("http://127.0.0.1:7897").port == 7897


def test_parse_proxy_url_rejects_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="scheme must be"):
        parse_proxy_url("ftp://127.0.0.1:21")


def test_resolve_proxy_selects_by_destination_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "HTTP_PROXY": "http://10.0.0.1:8080",
        "HTTPS_PROXY": "http://10.0.0.2:8443",
        "ALL_PROXY": "socks5://10.0.0.3:1080",
    }
    assert resolve_proxy("https", "a.example", env) is not None
    assert resolve_proxy("https", "a.example", env).host == "10.0.0.2"
    assert resolve_proxy("wss", "a.example", env).host == "10.0.0.2"
    assert resolve_proxy("http", "a.example", env).host == "10.0.0.1"
    assert resolve_proxy("ws", "a.example", env).host == "10.0.0.1"
    del env["HTTP_PROXY"], env["HTTPS_PROXY"]
    assert resolve_proxy("https", "a.example", env).scheme == "socks5"
    assert resolve_proxy("http", "a.example", env).scheme == "socks5"


def test_resolve_proxy_honors_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {"HTTPS_PROXY": "http://10.0.0.2:8443", "NO_PROXY": "other.example, .internal"}
    assert resolve_proxy("https", "center.example", env) is not None
    assert resolve_proxy("https", "center.INTERNAL", env) is None
    assert resolve_proxy("https", "x.internal", env) is None
    assert resolve_proxy("https", "internal.example", env) is not None
    env["NO_PROXY"] = "*"
    assert resolve_proxy("https", "anything.example", env) is None


def test_loopback_is_always_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.2:8443")
    assert resolve_proxy("https", "127.0.0.1", {}) is None
    assert resolve_proxy("https", "localhost", {}) is None
    assert is_loopback_host("127.5.0.1")
    assert not is_loopback_host("center.example")


# --------------------------------------------------------------------------- #
# Fake proxies
# --------------------------------------------------------------------------- #


class _Relay(threading.Thread):
    """One accepted proxy connection: relay bytes between client and target."""

    daemon = True

    def __init__(self, client: socket.socket, target: socket.socket) -> None:
        super().__init__(daemon=True)
        self._client = client
        self._target = target

    def run(self) -> None:
        pair = [self._client, self._target]
        try:
            while pair:
                readable, _, _ = select.select(pair, [], [], 5.0)
                if not readable:
                    continue
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (self._target if source is self._client else self._client).sendall(data)
        finally:
            for member in pair:
                member.close()


class FakeConnectProxy(threading.Thread):
    """A minimal HTTP CONNECT proxy: always relays to one fixed loopback target."""

    daemon = True

    def __init__(self, target: tuple[str, int], *, status: int = 200) -> None:
        super().__init__(daemon=True)
        self._target = target
        self._status = status
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = int(self.listener.getsockname()[1])

    def run(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            with client:
                head = bytearray()
                while b"\r\n\r\n" not in head:
                    chunk = client.recv(4096)
                    if not chunk:
                        return
                    head.extend(chunk)
                if self._status != 200:
                    client.sendall(
                        f"HTTP/1.1 {self._status} Denied\r\nContent-Length: 0\r\n\r\n".encode()
                    )
                    continue
                client.sendall(b"HTTP/1.1 200 Established\r\n\r\n")
                try:
                    target = socket.create_connection(self._target, timeout=5)
                except OSError:
                    return
                _Relay(client, target).run()


class FakeSocks5Proxy(FakeConnectProxy):
    """A minimal SOCKS5 proxy (no-auth), relaying to one fixed loopback target."""

    @staticmethod
    def _recv_exact(client: socket.socket, count: int) -> bytes:
        buffer = bytearray()
        while len(buffer) < count:
            chunk = client.recv(count - len(buffer))
            if not chunk:
                raise OSError("short read")
            buffer.extend(chunk)
        return bytes(buffer)

    def run(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            with client:
                try:
                    greeting = self._recv_exact(client, 2)  # ver, nmethods
                    if greeting[0] != 5:
                        return
                    self._recv_exact(client, greeting[1])  # drain method list
                    client.sendall(b"\x05\x00")
                    head = self._recv_exact(client, 5)  # ver cmd rsv atyp len
                    if head[3] != 3:
                        return
                    self._recv_exact(client, head[4])  # destination hostname
                    port = self._recv_exact(client, 2)
                    client.sendall(
                        b"\x05\x00\x00\x01\x7f\x00\x00\x01"
                        + int.from_bytes(port, "big").to_bytes(2, "big")
                    )
                    target = socket.create_connection(self._target, timeout=5)
                except OSError:
                    return
                _Relay(client, target).run()


class _Hello(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"hello-via-proxy"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Silence the default stderr access log."""


@pytest.fixture()
def hello_server() -> tuple[http.server.ThreadingHTTPServer, int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hello)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, int(server.server_address[1])
    server.shutdown()
    server.server_close()
    thread.join(timeout=5.0)


def _http_get_through(env: dict[str, str], target_port: int, host: str = FAKE_HOST) -> str:
    connection = http_connection_for(
        "http", host, target_port, timeout_s=5.0, env=env
    )
    connection.request("GET", "/")
    response = connection.getresponse()
    body = response.read().decode()
    connection.close()
    return body


def test_http_connection_through_connect_proxy(hello_server) -> None:
    _, target_port = hello_server
    proxy = FakeConnectProxy(("127.0.0.1", target_port))
    proxy.start()
    env = {"HTTP_PROXY": f"http://127.0.0.1:{proxy.port}"}
    assert _http_get_through(env, target_port) == "hello-via-proxy"


def test_http_connection_through_socks5_proxy(hello_server, monkeypatch) -> None:
    _, target_port = hello_server
    proxy = FakeSocks5Proxy(("127.0.0.1", target_port))
    proxy.start()
    env = {"ALL_PROXY": f"socks5://127.0.0.1:{proxy.port}"}
    assert _http_get_through(env, target_port) == "hello-via-proxy"


def test_connect_proxy_refusal_raises(hello_server, monkeypatch) -> None:
    _, target_port = hello_server
    proxy = FakeConnectProxy(("127.0.0.1", target_port), status=407)
    proxy.start()
    endpoint = parse_proxy_url(f"http://127.0.0.1:{proxy.port}")
    with pytest.raises(ProxyConnectError, match="refused CONNECT"):
        connect_via(endpoint, FAKE_HOST, target_port, timeout_s=5.0)


def test_websocket_dial_through_proxy(monkeypatch) -> None:
    harness = Harness()
    harness.start()
    try:
        proxy = FakeConnectProxy(("127.0.0.1", harness.port))
        proxy.start()
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.port}")
        from aipotluck.websocket import ClientConnection

        connection = ClientConnection.connect(
            f"ws://{FAKE_HOST}:{harness.port}/share", timeout=5.0
        )
        assert connection.peer
        connection.close()
    finally:
        harness.stop()
