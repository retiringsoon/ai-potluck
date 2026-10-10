"""Tests for the contribution-node connector.

Every session test runs over a real TCP loopback socket. The center side is a
real `http.server.ThreadingHTTPServer` whose handler calls
`aipotluck.websocket.handshake_server`, exactly as `tests/test_websocket.py` does;
the socket layer is never mocked, because the single-sender spool behaviour is
what is under test. The node database is a real SQLite file under `tmp_path`.

The center side records every message it receives in `Link.seen`, so a test can
assert the exact sequence of message types the node actually put on the wire.
"""

from __future__ import annotations

import datetime as dt
import http.server
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace

import pytest

from aipotluck.adapters import AdapterResult, FakeUpstreamAdapter
from aipotluck.canonical import encode_bytes
from aipotluck.connector import Connector, ConnectorConfig, redact
from aipotluck.fake_upstream import FakeResponse
from aipotluck.identity import IdentityRegistry, generate_key_pair, load_private_key
from aipotluck.metering import NormalizedUsage
from aipotluck.persistence import NodeStore
from aipotluck.records import SCHEMA_USAGE, SignedRecord, verify_chain
from aipotluck.websocket import (
    ServerConnection,
    WebSocketError,
    WebSocketTimeout,
    handshake_server,
)
from aipotluck.wire import (
    AttemptOffer,
    AttemptRef,
    Envelope,
    HelloAck,
    ResumeDirective,
    compute_permit_hash,
)

#: A fixed instant before every test key is registered, so signature validity
#: never depends on the wall clock.
REGISTERED_AT = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)

SECRET_TOKEN = "node-token-do-not-log-me"


# --------------------------------------------------------------------------- #
# Scripted center over a real socket
# --------------------------------------------------------------------------- #


@dataclass
class Link:
    """One accepted node session, with every received message recorded."""

    connection: ServerConnection
    seen: list[Envelope] = field(default_factory=list)

    def recv(self, timeout: float = 5.0) -> Envelope:
        """Receive and record one message."""
        envelope = Envelope.decode(self.connection.recv_text(timeout=timeout))
        self.seen.append(envelope)
        return envelope

    def send(self, envelope: Envelope) -> None:
        """Send one message to the node."""
        self.connection.send_text(envelope.encode())

    def recv_until(self, message_type: str, timeout: float = 5.0) -> Envelope:
        """Receive messages until one of `message_type` arrives."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"never received {message_type}; saw {self.types()}"
                )
            envelope = self.recv(timeout=remaining)
            if envelope.type == message_type:
                return envelope

    def types(self) -> list[str]:
        """Return the message types received so far, in order."""
        return [envelope.type for envelope in self.seen]


class Center:
    """A loopback WebSocket server that runs one script per node session."""

    def __init__(self, script: Callable[[int, Link], None]) -> None:
        self._script = script
        self._lock = threading.Lock()
        self._sessions = 0
        self.links: list[Link] = []
        self.errors: list[BaseException] = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    def _handler(self) -> type:
        center = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                """Silence the default access log."""

            def do_GET(self) -> None:
                try:
                    connection = handshake_server(self)
                except Exception as exc:
                    center.errors.append(exc)
                    self.close_connection = True
                    return
                with center._lock:
                    index = center._sessions
                    center._sessions += 1
                link = Link(connection)
                with center._lock:
                    center.links.append(link)
                try:
                    center._script(index, link)
                except BaseException as exc:
                    center.errors.append(exc)
                finally:
                    connection.close()

        return Handler

    @property
    def url(self) -> str:
        """The `ws://` URL the node should dial."""
        port = int(self.server.server_address[1])
        return f"ws://127.0.0.1:{port}/v1/node-link"

    def start(self) -> None:
        """Begin accepting sessions."""
        self.thread.start()

    def stop(self) -> None:
        """Stop the server and join its accept thread."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)

    def raise_errors(self) -> None:
        """Re-raise the first exception a session script raised."""
        if self.errors:
            raise self.errors[0]


@pytest.fixture
def center() -> Iterator[Callable[[Callable[[int, Link], None]], Center]]:
    """Return a factory that starts a scripted loopback center per call."""
    centers: list[Center] = []

    def start(script: Callable[[int, Link], None]) -> Center:
        instance = Center(script)
        instance.start()
        centers.append(instance)
        return instance

    yield start
    for instance in centers:
        instance.stop()


@pytest.fixture
def dead_end_port() -> Iterator[int]:
    """Return a TCP port that accepts a connection but never answers."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        yield int(sock.getsockname()[1])
    finally:
        sock.close()


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def make_config(
    tmp_path: object,
    center_url: str,
    adapter: FakeUpstreamAdapter | GatedAdapter,
    **overrides: object,
) -> ConnectorConfig:
    """Build a connector config pointing at `center_url`."""
    _, private_b64 = generate_key_pair()
    values: dict[str, object] = {
        "center_url": center_url,
        "node_id": "node-1",
        "device_id": "device-1",
        "device_key_id": "device-key-1",
        "provider_id": "provider-1",
        "database_path": f"{tmp_path}/node.db",
        "token": SECRET_TOKEN,
        "adapter_name": adapter.name,
        "model": adapter.model,
        "device_private_key_b64": private_b64,
        "delta_chars": 2,
    }
    values.update(overrides)
    return ConnectorConfig(**values)


def private_key_for(config: ConnectorConfig) -> object:
    """Return the Ed25519 private key the config would sign with."""
    return load_private_key(config.device_private_key_b64)


def make_offer(
    *,
    attempt_id: str = "A1",
    request_id: str = "R1",
    epoch: int = 1,
    model: str = "fake-model-v1",
    request_digest: str = "sha256:req",
    max_milli_credits: int = 1000,
    authorization_hash: str = "auth-1",
    prompt: str = "hi",
) -> AttemptOffer:
    """Build an offer whose `permit_hash` matches its own fields."""
    permit_hash = compute_permit_hash(
        request_id=request_id,
        attempt_id=attempt_id,
        epoch=epoch,
        model=model,
        request_digest=request_digest,
        max_milli_credits=max_milli_credits,
        authorization_hash=authorization_hash,
    )
    return AttemptOffer(
        request_id=request_id,
        attempt_id=attempt_id,
        epoch=epoch,
        resource_id="res-1",
        model=model,
        request_digest=request_digest,
        max_milli_credits=max_milli_credits,
        deadline_ms=1000,
        delivery_mode="buffered_complete",
        prompt=prompt,
        authorization_hash=authorization_hash,
        permit_hash=permit_hash,
    )


def hello_ack(resume: tuple[ResumeDirective, ...] = ()) -> Envelope:
    """Build a `hello.ack` with the given resume directives."""
    return Envelope(
        type="hello.ack",
        payload=HelloAck(
            node_id="node-1",
            server_time=dt.datetime.now(dt.UTC),
            heartbeat_interval_s=15,
            resume=resume,
        ).to_payload(),
    )


def execute(offer: AttemptOffer) -> Envelope:
    """Build the `attempt.execute` for an offer."""
    return Envelope(
        type="attempt.execute",
        payload=AttemptRef(offer.request_id, offer.attempt_id, offer.epoch).to_payload(),
    )


def ack(attempt_id: str, acked_seq: int) -> Envelope:
    """Build an `event.ack` for a contiguous prefix."""
    return Envelope(type="event.ack", payload={"attempt_id": attempt_id, "acked_seq": acked_seq})


def record_from_dict(body: dict[str, object]) -> SignedRecord:
    """Rebuild the signed record a `usage.final` report carries."""
    return SignedRecord(
        schema=str(body["schema"]),
        record_id=str(body["record_id"]),
        revision=int(str(body["revision"])),
        signed_at=dt.datetime.fromisoformat(str(body["signed_at"])),
        signer_key_id=str(body["signer_key_id"]),
        signer_id=str(body["signer_id"]),
        signature=str(body["signature"]),
        payload=body["payload"],
        previous_record_hash=body["previous_record_hash"],
    )


def wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """Poll `predicate` until it is true or fail the test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


@dataclass
class GatedAdapter:
    """An adapter whose single call blocks until the test releases it."""

    name: str = "gated-upstream"
    model: str = "gated-model"
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    calls: int = 0

    def complete(self, prompt: str, *, request_digest: str) -> AdapterResult:
        """Block until released, then return a completion."""
        del prompt, request_digest
        self.calls += 1
        self.entered.set()
        self.release.wait(10.0)
        return AdapterResult(
            chunks=("done",),
            usage=NormalizedUsage(input_total=1, output_total=1),
            provider_style="openai",
            upstream_calls=self.calls,
        )


# --------------------------------------------------------------------------- #
# 1. HELLO
# --------------------------------------------------------------------------- #


def test_hello_advertises_open_attempts_and_empty_resume_handled(center, tmp_path) -> None:
    """HELLO carries the open execution and device key; an empty ack is handled."""
    adapter = FakeUpstreamAdapter()
    store = NodeStore.open(str(tmp_path / "node.db"))
    store.begin_execution(
        attempt_id="attempt-1",
        request_id="req-1",
        epoch=3,
        request_digest="sha256:seed",
        at=dt.datetime.now(dt.UTC),
    )
    observed: dict[str, object] = {}

    def script(index: int, link: Link) -> None:
        del index
        hello = link.recv()
        assert hello.type == "node.hello"
        observed["payload"] = hello.payload
        link.send(hello_ack())

    instance = center(script)
    config = make_config(tmp_path, instance.url, adapter)
    connector = Connector(config, adapter=adapter, store=store)
    connector.run_once()

    instance.raise_errors()
    payload = observed["payload"]
    assert payload["device_key_id"] == "device-key-1"
    assert payload["open_attempts"] == ["attempt-1"]
    assert payload["adapter"] == {"name": "fake-upstream", "model": "fake-model-v1"}
    assert payload["policy"]["share_enabled"] is True


# --------------------------------------------------------------------------- #
# 2. Happy path
# --------------------------------------------------------------------------- #


def test_happy_path_over_a_real_socket(center, tmp_path) -> None:
    """Offer → accept → execute → deltas → usage → completed, all acked."""
    adapter = FakeUpstreamAdapter()
    adapter.upstream.queue(
        FakeResponse(text_chunks=("Hello",), usage=NormalizedUsage(input_total=10, output_total=5))
    )
    offer = make_offer(prompt="hi")
    observed: dict[str, object] = {}

    def script(index: int, link: Link) -> None:
        del index
        hello = link.recv()
        assert hello.payload["open_attempts"] == []
        link.send(hello_ack())
        link.send(Envelope("attempt.offer", offer.to_payload()))
        accept = link.recv_until("attempt.accept")
        observed["acceptance_hash"] = accept.payload["acceptance_hash"]
        assert accept.payload["permit_hash"] == offer.permit_hash
        link.send(execute(offer))
        events: list[Envelope] = []
        while True:
            envelope = link.recv_until("attempt.event")
            events.append(envelope)
            if envelope.payload["event_type"] == "attempt.completed":
                break
        highest = max(int(event.payload["seq"]) for event in events)
        link.send(ack(offer.attempt_id, highest))
        time.sleep(0.2)
        observed["events"] = events

    instance = center(script)
    config = make_config(tmp_path, instance.url, adapter)
    connector = Connector(config, adapter=adapter)
    connector.run_once()

    instance.raise_errors()
    assert connector.upstream_call_count == 1
    assert adapter.upstream_calls == 1
    assert instance.links[0].types() == [
        "node.hello",
        "attempt.accept",
        "attempt.event",
        "attempt.event",
        "attempt.event",
        "attempt.event",
        "attempt.event",
    ]
    events = observed["events"]
    assert [event.payload["event_type"] for event in events] == [
        "output.delta",
        "output.delta",
        "output.delta",
        "usage.final",
        "attempt.completed",
    ]
    assert [int(event.payload["seq"]) for event in events] == [1, 2, 3, 4, 5]
    assert "".join(str(event.payload["text"]) for event in events[:3]) == "Hello"
    assert connector.store.unacked_spool("A1") == []

    usage_payload = events[3].payload
    assert usage_payload["usage"]["input_total"] == 10
    assert usage_payload["usage"]["output_total"] == 5
    report_body = usage_payload["report"]
    assert report_body["schema"] == SCHEMA_USAGE
    assert report_body["payload"]["attempt_id"] == "A1"
    assert report_body["payload"]["permit_hash"] == offer.permit_hash
    assert report_body["payload"]["acceptance_hash"] == observed["acceptance_hash"]
    assert report_body["payload"]["measurement"]["usage"]["input_total"] == 10
    assert (
        report_body["payload"]["measurement"]["usage"]["input_total"]
        + report_body["payload"]["measurement"]["usage"]["output_total"]
        == usage_payload["usage"]["input_total"] + usage_payload["usage"]["output_total"]
    )

    # The signed report must verify against the device key the node holds.
    registry = IdentityRegistry()
    public_b64 = encode_bytes(private_key_for(config).public_key().public_bytes_raw())
    registry.register_device_key(
        "device-key-1", "device-1", "provider-1", public_b64, REGISTERED_AT
    )
    verify_chain(
        registry,
        record_from_dict(report_body),
        "provider_device",
        owner_id="device-1",
        witnessed_at=dt.datetime.now(dt.UTC),
    )
    assert connector.store.get_execution("A1").upstream_calls == 1
    assert connector.store.get_execution("A1").state == "SUBMITTED"


# --------------------------------------------------------------------------- #
# 3. Duplicate EXECUTE must not re-run the adapter
# --------------------------------------------------------------------------- #


def test_duplicate_execute_does_not_call_the_adapter_again(center, tmp_path) -> None:
    """A second EXECUTE for one attempt re-sends the spool, never re-executes."""
    adapter = FakeUpstreamAdapter()
    adapter.upstream.queue(
        FakeResponse(text_chunks=("abc",), usage=NormalizedUsage(input_total=3, output_total=4))
    )
    offer = make_offer(prompt="hi")
    observed: dict[str, object] = {}

    def script(index: int, link: Link) -> None:
        del index
        link.recv()
        link.send(hello_ack())
        link.send(Envelope("attempt.offer", offer.to_payload()))
        link.recv_until("attempt.accept")
        link.send(execute(offer))
        first: list[Envelope] = []
        while True:
            envelope = link.recv_until("attempt.event")
            first.append(envelope)
            if envelope.payload["event_type"] == "attempt.completed":
                break
        observed["first"] = first
        # Acknowledge only the deltas, leaving usage and completed unacked.
        for envelope in first:
            if envelope.payload["event_type"] == "output.delta":
                link.send(ack(offer.attempt_id, int(envelope.payload["seq"])))
        time.sleep(0.1)
        # Duplicate EXECUTE #1: the node must replay the unacked spool and must
        # not run the adapter a second time. The script stays tolerant so the
        # main thread can assert concrete values instead of timing out.
        link.send(execute(offer))
        resent: list[Envelope] = []
        while len(resent) < 2:
            try:
                envelope = link.recv(timeout=1.5)
            except WebSocketTimeout:
                break
            if envelope.type != "attempt.event":
                break
            resent.append(envelope)
        observed["resent"] = resent
        for envelope in resent:
            link.send(ack(offer.attempt_id, int(envelope.payload["seq"])))
        time.sleep(0.1)
        # Duplicate EXECUTE #2: nothing left to replay, so report real state.
        link.send(execute(offer))
        try:
            state = link.recv(timeout=1.5)
        except WebSocketTimeout:
            state = None
        observed["state"] = None if state is None else state.payload

    instance = center(script)
    config = make_config(tmp_path, instance.url, adapter)
    connector = Connector(config, adapter=adapter)
    connector.run_once()

    # The load-bearing assertion: exactly one upstream call happened, so the
    # duplicate EXECUTE neither re-ran the adapter nor burned the script.
    assert adapter.upstream_calls == 1
    assert connector.upstream_call_count == 1
    instance.raise_errors()
    first = observed["first"]
    assert [envelope.payload["event_type"] for envelope in first] == [
        "output.delta",
        "output.delta",
        "usage.final",
        "attempt.completed",
    ]
    resent = observed["resent"]
    assert [envelope.payload["event_type"] for envelope in resent] == [
        "usage.final",
        "attempt.completed",
    ]
    assert [int(envelope.payload["seq"]) for envelope in resent] == [3, 4]
    assert observed["state"] is not None
    assert observed["state"]["state"] == "SUBMITTED"
    assert observed["state"]["certainty"] == "submitted"
    assert observed["state"]["upstream_calls"] == 1


# --------------------------------------------------------------------------- #
# 4. Permit mismatch
# --------------------------------------------------------------------------- #


def test_offer_with_mismatched_permit_is_rejected(tmp_path) -> None:
    """A permit hash that disagrees with the offer's terms is refused."""
    adapter = FakeUpstreamAdapter()
    config = make_config(tmp_path, "ws://127.0.0.1:1/v1/node-link", adapter)
    connector = Connector(config, adapter=adapter)
    offer = make_offer()
    tampered = replace(offer, permit_hash="sha256:" + "0" * 64)

    outgoing = connector.handle_message(Envelope("attempt.offer", tampered.to_payload()))

    assert [envelope.type for envelope in outgoing] == ["attempt.reject"]
    assert "permit" in str(outgoing[0].payload["reason"])
    assert connector.store.get_execution("A1") is None
    assert connector.upstream_call_count == 0
    assert adapter.upstream_calls == 0


# --------------------------------------------------------------------------- #
# 5. Local policy refusal
# --------------------------------------------------------------------------- #


def test_offer_refused_when_sharing_is_paused(tmp_path) -> None:
    """`share_enabled=False` refuses every offer without an execution row."""
    adapter = FakeUpstreamAdapter()
    config = make_config(tmp_path, "ws://127.0.0.1:1/v1/node-link", adapter, share_enabled=False)
    connector = Connector(config, adapter=adapter)

    outgoing = connector.handle_message(Envelope("attempt.offer", make_offer().to_payload()))

    assert [envelope.type for envelope in outgoing] == ["attempt.reject"]
    assert "paused" in str(outgoing[0].payload["reason"])
    assert connector.store.get_execution("A1") is None
    assert connector.upstream_call_count == 0
    assert adapter.upstream_calls == 0


def test_offer_refused_when_concurrency_is_full(tmp_path) -> None:
    """With one execution in flight, the next offer is refused locally."""
    adapter = GatedAdapter()
    config = make_config(tmp_path, "ws://127.0.0.1:1/v1/node-link", adapter, max_concurrency=1)
    connector = Connector(config, adapter=adapter)
    first = make_offer(attempt_id="A1", request_id="R1")
    second = make_offer(attempt_id="A2", request_id="R2")

    accepted = connector.handle_message(Envelope("attempt.offer", first.to_payload()))
    assert [envelope.type for envelope in accepted] == ["attempt.accept"]
    assert connector.handle_message(execute(first)) == []
    assert adapter.entered.wait(5.0), "the first execution never reached the adapter"

    refused = connector.handle_message(Envelope("attempt.offer", second.to_payload()))
    assert [envelope.type for envelope in refused] == ["attempt.reject"]
    assert "concurrency" in str(refused[0].payload["reason"])
    assert connector.upstream_call_count == 0
    assert connector.store.get_execution("A2") is None

    adapter.release.set()
    wait_until(lambda: execution_finished(connector, "A1"))


# --------------------------------------------------------------------------- #
# 6. Reconnect resends the spool and never re-executes
# --------------------------------------------------------------------------- #


def test_reconnect_resends_unacked_spool_without_reexecuting(center, tmp_path) -> None:
    """A session that ends mid-stream resumes from the spool on the next dial."""
    adapter = FakeUpstreamAdapter()
    adapter.upstream.queue(
        FakeResponse(text_chunks=("abcdef",), usage=NormalizedUsage(input_total=2, output_total=3))
    )
    offer = make_offer(attempt_id="A1", request_id="R1", epoch=1)
    observed: dict[str, object] = {}

    def script(index: int, link: Link) -> None:
        hello = link.recv()
        assert hello.type == "node.hello"
        if index == 0:
            link.send(hello_ack())
            link.send(Envelope("attempt.offer", offer.to_payload()))
            link.recv_until("attempt.accept")
            link.send(execute(offer))
            first = link.recv_until("attempt.event")
            assert first.payload["event_type"] == "output.delta"
            link.send(ack("A1", int(first.payload["seq"])))
            return
        link.send(
            hello_ack(
                (
                    ResumeDirective(
                        request_id="R1", attempt_id="A1", epoch=1, action="resend_spool"
                    ),
                )
            )
        )
        events: list[Envelope] = []
        state: dict[str, object] | None = None
        while state is None:
            envelope = link.recv()
            if envelope.type == "attempt.event":
                events.append(envelope)
            elif envelope.type == "attempt.state":
                state = envelope.payload
            else:
                raise AssertionError(f"unexpected {envelope.type}")
        link.send(ack("A1", max(int(event.payload["seq"]) for event in events)))
        time.sleep(0.1)
        observed["events"] = events
        observed["state"] = state

    instance = center(script)
    config = make_config(tmp_path, instance.url, adapter)
    connector = Connector(config, adapter=adapter)
    connector.run_once()
    wait_until(lambda: execution_finished(connector, "A1"))
    connector.run_once()

    instance.raise_errors()
    assert connector.upstream_call_count == 1
    assert adapter.upstream_calls == 1
    events = observed["events"]
    assert [envelope.payload["event_type"] for envelope in events] == [
        "output.delta",
        "output.delta",
        "usage.final",
        "attempt.completed",
    ]
    assert [int(envelope.payload["seq"]) for envelope in events] == [2, 3, 4, 5]
    assert observed["state"]["state"] == "SUBMITTED"
    assert observed["state"]["upstream_calls"] == 1
    assert connector.store.unacked_spool("A1") == []


# --------------------------------------------------------------------------- #
# 7. Restart with a killed execution
# --------------------------------------------------------------------------- #


def test_restart_reports_unknown_rather_than_zero(center, tmp_path, caplog) -> None:
    """A SUBMITTING row left by a crash becomes UNKNOWN and is never re-run."""
    adapter = FakeUpstreamAdapter()
    store = NodeStore.open(str(tmp_path / "node.db"))
    store.begin_execution(
        attempt_id="A9",
        request_id="R9",
        epoch=2,
        request_digest="sha256:seed",
        at=dt.datetime.now(dt.UTC),
    )
    assert store.get_execution("A9").state == "SUBMITTING"
    observed: dict[str, object] = {}

    def script(index: int, link: Link) -> None:
        del index
        hello = link.recv()
        assert hello.payload["open_attempts"] == ["A9"]
        link.send(hello_ack())
        link.send(Envelope("attempt.query", {"request_id": "R9", "attempt_id": "A9"}))
        observed["state"] = link.recv_until("attempt.state").payload

    instance = center(script)
    config = make_config(tmp_path, instance.url, adapter)
    with caplog.at_level(logging.WARNING, logger="aipotluck.connector"):
        connector = Connector(config, adapter=adapter, store=store)

    assert store.get_execution("A9").state == "UNKNOWN"
    assert "A9" in caplog.text

    connector.run_once()

    instance.raise_errors()
    assert observed["state"]["state"] == "UNKNOWN"
    assert observed["state"]["certainty"] == "unknown"
    assert observed["state"]["upstream_calls"] == 0
    assert adapter.upstream_calls == 0


# --------------------------------------------------------------------------- #
# 8. Heartbeat
# --------------------------------------------------------------------------- #


def test_heartbeat_is_emitted_when_idle(center, tmp_path) -> None:
    """After the idle interval the node sends a liveness heartbeat."""
    adapter = FakeUpstreamAdapter()
    observed: dict[str, object] = {}

    def script(index: int, link: Link) -> None:
        del index
        link.recv()
        link.send(hello_ack())
        observed["heartbeat"] = link.recv_until("node.heartbeat", timeout=5.0).payload

    instance = center(script)
    config = make_config(tmp_path, instance.url, adapter, heartbeat_interval_s=1)
    connector = Connector(config, adapter=adapter)
    connector.run_once()

    instance.raise_errors()
    assert observed["heartbeat"]["share_enabled"] is True
    assert observed["heartbeat"]["in_flight"] == 0


# --------------------------------------------------------------------------- #
# 9. The token never leaks
# --------------------------------------------------------------------------- #


def test_redact_masks_token_and_failure_path_is_scribable(dead_end_port, tmp_path) -> None:
    """`redact` masks the credential and the connect failure echoes no token."""
    adapter = FakeUpstreamAdapter()
    url = f"ws://127.0.0.1:{dead_end_port}/v1/node-link"
    config = make_config(tmp_path, url, adapter, connect_timeout_s=0.5)

    view = redact(config)
    assert view["token"] == "***"
    assert view["device_private_key_b64"] == "***"
    assert config.token not in repr(view)
    assert config.device_private_key_b64 not in repr(view)

    connector = Connector(config, adapter=adapter)
    with pytest.raises(WebSocketError) as excinfo:
        connector.run_once()
    assert config.token not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 10. `stop` during backoff
# --------------------------------------------------------------------------- #


def test_run_forever_logs_no_token_and_stops_promptly(dead_end_port, tmp_path, caplog) -> None:
    """`stop` ends a backoff promptly and the failure log carries no token."""
    adapter = FakeUpstreamAdapter()
    url = f"ws://127.0.0.1:{dead_end_port}/v1/node-link"
    config = make_config(
        tmp_path,
        url,
        adapter,
        reconnect_backoff_s=(30.0,),
        connect_timeout_s=0.5,
    )
    connector = Connector(config, adapter=adapter)

    with caplog.at_level(logging.WARNING, logger="aipotluck.connector"):
        thread = threading.Thread(target=connector.run_forever, daemon=True)
        thread.start()
        time.sleep(1.2)  # the first attempt fails, then the loop waits 30s
        started = time.monotonic()
        connector.stop()
        thread.join(timeout=2.0)
        elapsed = time.monotonic() - started

    assert not thread.is_alive()
    assert elapsed < 2.0
    assert config.token not in caplog.text


def execution_finished(connector: Connector, attempt_id: str) -> bool:
    """Whether the node has finished the named execution."""
    record = connector.store.get_execution(attempt_id)
    return record is not None and record.finished_at is not None
