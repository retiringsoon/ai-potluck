"""Outbound proxy support for every dial this package makes.

The consumer/client, the node-link WebSocket, and the upstream adapter all
dial raw sockets, so proxies must be honored at the socket layer rather than
by rewriting URLs. Resolution follows the de-facto standard (curl/requests):

- destination ``https``/``wss`` -> ``HTTPS_PROXY`` (then ``ALL_PROXY``)
- destination ``http``/``ws``   -> ``HTTP_PROXY``  (then ``ALL_PROXY``)
- ``NO_PROXY`` excludes destinations (``*``, exact hosts, suffix domains,
  IP literals); loopback destinations are always direct
- proxy URLs: ``http://[user:pass@]host:port``, ``https://...`` (TLS to the
  proxy), ``socks5://...`` (remote DNS: the hostname is sent to the proxy,
  i.e. socks5h semantics)

Both relay kinds are implemented here as a plain CONNECT-ed socket, so the
same tunnel serves the hand-rolled WebSocket handshake and ``http.client``
(whose ``HTTPConnection`` accepts a pre-connected socket through
``_create_connection``).
"""

from __future__ import annotations

import base64
import http.client
import os
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

__all__ = [
    "ProxyConnectError",
    "ProxyEndpoint",
    "connect_via",
    "http_connection_for",
    "is_loopback_host",
    "parse_proxy_url",
    "resolve_proxy",
]


class ProxyConnectError(OSError):
    """The proxy refused or failed the relay (non-2xx CONNECT, SOCKS5 error)."""


_SOCKS5_REPLIES = {
    1: "general SOCKS server failure",
    2: "connection not allowed by ruleset",
    3: "network unreachable",
    4: "host unreachable",
    5: "connection refused",
    6: "TTL expired",
    7: "command not supported",
    8: "address type not supported",
}

_HTTPS_ENV_NAMES = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
_HTTP_ENV_NAMES = ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
_DEFAULT_PORTS = {"http": 80, "https": 443, "socks5": 1080}


@dataclass(frozen=True)
class ProxyEndpoint:
    """A parsed proxy URL. ``scheme`` is one of ``http``, ``https``, ``socks5``."""

    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None


def parse_proxy_url(url: str) -> ProxyEndpoint:
    """Parse ``scheme://[user:pass@]host:port`` into a `ProxyEndpoint`."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError(
            f"proxy URL scheme must be http, https, or socks5, not {parts.scheme!r}"
        )
    host = parts.hostname
    if not host:
        raise ValueError(f"proxy URL has no host: {url!r}")
    return ProxyEndpoint(
        scheme=scheme,
        host=host,
        port=parts.port or _DEFAULT_PORTS[scheme],
        username=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password else None,
    )


def is_loopback_host(host: str) -> bool:
    """Whether the destination is this machine; loopback is always direct."""
    lowered = host.strip("[]").lower()
    return lowered in ("localhost", "::1") or lowered.startswith("127.")


def _no_proxy_matches(host: str, no_proxy: str) -> bool:
    """Standard ``NO_PROXY`` matching: ``*``, exact hosts, suffix domains, IPs."""
    lowered = host.strip("[]").lower()
    for raw in no_proxy.split(","):
        entry = raw.strip().lower().rstrip(".")
        if not entry:
            continue
        if entry == "*":
            return True
        if lowered == entry:
            return True
        if entry.startswith(".") and lowered.endswith(entry):
            return True
        if lowered.endswith("." + entry):
            return True
    return False


def resolve_proxy(
    destination_scheme: str, host: str, env: dict[str, str] | None = None
) -> ProxyEndpoint | None:
    """Resolve the proxy for a dial to ``host`` with scheme ``destination_scheme``.

    ``destination_scheme`` is one of ``http``, ``https``, ``ws``, ``wss``.
    Returns `None` when the destination is loopback, excluded by ``NO_PROXY``,
    or no proxy variable applies.
    """
    environment = os.environ if env is None else env
    if is_loopback_host(host):
        return None
    no_proxy = environment.get("NO_PROXY") or environment.get("no_proxy") or ""
    if no_proxy and _no_proxy_matches(host, no_proxy):
        return None
    lowered = destination_scheme.lower()
    names = _HTTPS_ENV_NAMES if lowered in ("https", "wss") else _HTTP_ENV_NAMES
    for name in names:
        value = environment.get(name, "").strip()
        if value:
            return parse_proxy_url(value)
    return None


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    """Read exactly ``count`` bytes or fail the connection."""
    buffer = bytearray()
    while len(buffer) < count:
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            raise ProxyConnectError("proxy closed the connection mid-reply")
        buffer.extend(chunk)
    return bytes(buffer)


def _read_http_head(sock: socket.socket) -> bytes:
    """Read one HTTP head, up to and including the blank line (bounded 16 KiB)."""
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        if len(buffer) > 16_384:
            raise ProxyConnectError("proxy sent an oversized CONNECT reply")
        chunk = sock.recv(4096)
        if not chunk:
            raise ProxyConnectError("proxy closed the connection mid-reply")
        buffer.extend(chunk)
    return bytes(buffer[: buffer.index(b"\r\n\r\n") + 4])


def _connect_http_proxy(
    sock: socket.socket, endpoint: ProxyEndpoint, host: str, port: int
) -> None:
    """Send ``CONNECT host:port`` and require a 2xx reply."""
    lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
    if endpoint.username is not None:
        raw = f"{endpoint.username}:{endpoint.password or ''}".encode()
        token = base64.b64encode(raw).decode("ascii")
        lines.append(f"Proxy-Authorization: Basic {token}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    head = _read_http_head(sock).decode("latin-1")
    status_line = head.split("\r\n", 1)[0]
    fields = status_line.split(" ", 2)
    if len(fields) < 2 or not fields[1].startswith("2"):
        raise ProxyConnectError(f"proxy refused CONNECT: {status_line!r}")


def _connect_socks5(
    sock: socket.socket, endpoint: ProxyEndpoint, host: str, port: int
) -> None:
    """Complete a SOCKS5 handshake (socks5h: the proxy resolves ``host``)."""
    methods = b"\x00" if endpoint.username is None else b"\x00\x02"
    sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
    reply = _recv_exact(sock, 2)
    if reply[0] != 5:
        raise ProxyConnectError(f"not a SOCKS5 proxy (reply version {reply[0]})")
    method = reply[1]
    if method == 0xFF:
        raise ProxyConnectError("proxy accepts none of our SOCKS5 auth methods")
    if method == 0x02:
        username = (endpoint.username or "").encode("utf-8")
        password = (endpoint.password or "").encode("utf-8")
        sock.sendall(
            b"\x01"
            + bytes([len(username)])
            + username
            + bytes([len(password)])
            + password
        )
        status = _recv_exact(sock, 2)
        if status[1] != 0:
            raise ProxyConnectError("SOCKS5 username/password auth rejected")
    elif method != 0x00:
        raise ProxyConnectError(f"proxy chose unsupported SOCKS5 method {method}")
    literal = host.strip("[]")
    try:
        host_bytes = literal.encode("ascii")
    except UnicodeEncodeError:
        host_bytes = literal.encode("idna")  # internationalized domain -> punycode
    sock.sendall(
        b"\x05\x01\x00\x03"
        + bytes([len(host_bytes)])
        + host_bytes
        + int(port).to_bytes(2, "big")
    )
    reply = _recv_exact(sock, 4)
    if reply[0] != 5:
        raise ProxyConnectError(f"not a SOCKS5 proxy (reply version {reply[0]})")
    if reply[1] != 0:
        reason = _SOCKS5_REPLIES.get(reply[1], f"unknown error {reply[1]}")
        raise ProxyConnectError(f"SOCKS5 CONNECT failed: {reason}")
    atyp = reply[3]
    skip = {1: 4, 4: 16}.get(atyp)
    if skip is None:
        raise ProxyConnectError(f"SOCKS5 proxy returned unsupported address type {atyp}")
    if atyp == 3:
        skip = _recv_exact(sock, 1)[0]
    _recv_exact(sock, skip + 2)  # bound address + port


def connect_via(
    endpoint: ProxyEndpoint, host: str, port: int, timeout_s: float
) -> socket.socket:
    """Open a raw socket to ``host:port`` relayed through ``endpoint``.

    The returned socket is connected end to end but not TLS-wrapped; the
    caller wraps TLS exactly as it would for a direct dial.
    """
    sock = socket.create_connection((endpoint.host, endpoint.port), timeout=timeout_s)
    try:
        if endpoint.scheme == "https":
            tls = ssl.create_default_context()
            sock = tls.wrap_socket(sock, server_hostname=endpoint.host)
        if endpoint.scheme == "socks5":
            _connect_socks5(sock, endpoint, host, port)
        else:
            _connect_http_proxy(sock, endpoint, host, port)
    except BaseException:
        sock.close()
        raise
    return sock


def http_connection_for(
    destination_scheme: str,
    host: str,
    port: int,
    *,
    timeout_s: float,
    tls_context: ssl.SSLContext | None = None,
    env: dict[str, str] | None = None,
) -> http.client.HTTPConnection:
    """Build the ``http.client`` connection for a destination, honoring proxies.

    Without a proxy this is the plain ``HTTP(S)Connection`` the callers built
    before. With one, the connection targets the proxy and a pre-connected
    tunnel socket is injected through ``_create_connection`` (the CPython
    internal that ``HTTPConnection.connect`` calls; stable across 3.x), so
    request encoding, TLS wrapping, and response parsing stay stdlib.
    """
    destination = destination_scheme.lower()
    secure = destination in ("https", "wss")
    proxy = resolve_proxy(destination, host, env)
    if proxy is None:
        if secure:
            return http.client.HTTPSConnection(
                context=tls_context, host=host, port=port, timeout=timeout_s
            )
        return http.client.HTTPConnection(host, port, timeout=timeout_s)

    def tunneled_connect(
        address: tuple[str, int],
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        del address, source_address
        return connect_via(proxy, host, port, timeout_s)

    connection: http.client.HTTPConnection
    if secure:
        connection = http.client.HTTPSConnection(
            context=tls_context, host=host, port=port, timeout=timeout_s
        )
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout_s)
    connection._create_connection = tunneled_connect  # type: ignore[attr-defined]
    return connection
