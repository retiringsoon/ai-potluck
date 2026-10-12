"""Contribution-node connector: the process an operator runs to share a model.

The connector dials the center over one outbound WebSocket and, on that single
link, offers the node's local policy, accepts or refuses offers, executes
accepted attempts against the operator's own upstream credential, and reports
signed usage.

Single-sender design
--------------------
The socket loop is the *only* writer to the wire. Executions run on worker
threads that write their events into the node's SQLite spool; the socket loop
drains that spool in ``(attempt_id, attempt_seq)`` order. This is not a style
preference:

* It makes the spool the real mechanism rather than a decorative log. A link
  that drops after an upstream call has already happened still holds every
  produced event durably, because the worker wrote it before the socket ever
  saw it. On reconnect the same rows are re-sent.
* It gives the wire exactly one ordered writer, so the center's
  ``event.ack`` cursors mean something. If a worker could send directly, two
  threads could interleave frames and a contiguous-prefix ACK would no longer
  describe the stream.
* It keeps a slow or broken socket from blocking an execution, and a slow
  upstream call from blocking the control channel.

`handle_message` is deliberately pure with respect to the socket: it reads and
writes the node store and may start a worker, but it never touches a
connection; it returns the envelopes the caller should send. That is what makes
the protocol testable without a socket, and it is enforced by the type: every
send in the session loop goes through the loop thread.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from .adapters import (
    Adapter,
    AdapterError,
    AdapterOutcomeUnknown,
    AdapterQuotaExceeded,
    StreamChunk,
    chunk_text,
)
from .canonical import loads_strict
from .coordinator import LocalPolicy
from .errors import PotluckError, ProtocolError, StateTransitionError
from .identity import load_private_key
from .metering import Measurement, NormalizedUsage
from .persistence import ExecutionRecord, NodeStore, SpoolRecord
from .records import SignedRecord
from .settlement import build_usage_report
from .websocket import ClientConnection, WebSocketClosed, WebSocketTimeout
from .wire import (
    AttemptDecision,
    AttemptEvent,
    AttemptOffer,
    AttemptRef,
    AttemptState,
    Envelope,
    EventAck,
    ExecutionCertainty,
    ExecutionState,
    HelloAck,
    NodeHeartbeat,
    NodeHello,
    NodePolicy,
    ProtocolErrorBody,
    ResumeDirective,
    compute_acceptance_hash,
    compute_permit_hash,
    error_envelope,
)

logger = logging.getLogger(__name__)

#: Reconnect backoff, clamped at its last value.
DEFAULT_RECONNECT_BACKOFF_S = (0.5, 1.0, 2.0, 5.0, 10.0)

#: Replaces the platform credential in every log-safe view.
_TOKEN_MASK = "***"

#: How long the socket loop blocks in `recv_text` before re-checking the spool
#: and the heartbeat clock. Bounds the wait for an ACK: spooled events are sent
#: once per iteration, and only `store.ack_through` retires them.
_POLL_INTERVAL_S = 0.05

_STATE_VALUES = frozenset({"SUBMITTING", "SUBMITTED", "FAILED", "UNKNOWN"})


def _now() -> dt.datetime:
    """Return the current timezone-aware UTC instant."""
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True)
class ConnectorConfig:
    """Everything an operator configures to join the platform as a node."""

    center_url: str
    node_id: str
    device_id: str
    device_key_id: str
    provider_id: str
    database_path: str
    token: str
    adapter_name: str
    model: str
    device_private_key_b64: str
    share_enabled: bool = True
    max_concurrency: int = 1
    max_local_reserved_milli_credits: int = 20_000
    heartbeat_interval_s: int = 15
    delta_chars: int = 4096
    connect_timeout_s: float = 10.0
    reconnect_backoff_s: tuple[float, ...] = DEFAULT_RECONNECT_BACKOFF_S
    max_concurrent_executions: int = 2
    #: Path to the node's own JSON configuration, when the node was started from
    #: one. It is the file `potluck share pause`/`resume` rewrites; the connector
    #: polls it so a running node picks the change up without a restart. `None`
    #: disables polling (a node configured programmatically, as tests do).
    config_path: str | None = None
    #: How often the connector re-reads the share switch from `config_path`.
    #: This is the pause/resume latency, so it is short; the cost is one small
    #: file read plus a stat per interval, which is negligible next to a
    #: heartbeat.
    policy_poll_interval_s: float = 1.0


def redact(config: ConnectorConfig) -> dict[str, object]:
    """Return a log-safe view of the config with the token masked.

    The platform credential never appears in a log line, an exception message,
    or a diagnostic dump; this is the only supported way to render a config.
    """
    return {
        "center_url": config.center_url,
        "node_id": config.node_id,
        "device_id": config.device_id,
        "device_key_id": config.device_key_id,
        "provider_id": config.provider_id,
        "database_path": config.database_path,
        "token": _TOKEN_MASK,
        "adapter_name": config.adapter_name,
        "model": config.model,
        "device_private_key_b64": _TOKEN_MASK,
        "share_enabled": config.share_enabled,
        "max_concurrency": config.max_concurrency,
        "max_local_reserved_milli_credits": config.max_local_reserved_milli_credits,
        "heartbeat_interval_s": config.heartbeat_interval_s,
        "delta_chars": config.delta_chars,
        "connect_timeout_s": config.connect_timeout_s,
        "reconnect_backoff_s": list(config.reconnect_backoff_s),
        "max_concurrent_executions": config.max_concurrent_executions,
        "config_path": config.config_path,
        "policy_poll_interval_s": config.policy_poll_interval_s,
    }


@dataclass(frozen=True)
class _AcceptedOffer:
    """An offer this node accepted, kept so EXECUTE need not re-derive terms."""

    offer: AttemptOffer
    acceptance_hash: str


@dataclass
class _State:
    """Mutable connector state, grouped so the lock discipline is explicit.

    Work done on the socket-loop thread and on worker threads overlaps here, so
    every field is only touched while holding `lock`.
    """

    accepted: dict[str, _AcceptedOffer] = field(default_factory=dict)
    workers: dict[str, threading.Thread] = field(default_factory=dict)
    cancel_events: dict[str, threading.Event] = field(default_factory=dict)
    tracked: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)


class Connector:
    """The contribution node's link to the center.

    Args:
        config: Operator configuration, including the platform credential.
        adapter: The one fixed upstream this node executes against.
        store: The node's durable store. Defaults to a store at
            `config.database_path`; tests may inject a store over the same
            database to prove restart behaviour.
    """

    def __init__(
        self,
        config: ConnectorConfig,
        *,
        adapter: Adapter,
        store: NodeStore | None = None,
    ) -> None:
        self._config = config
        self._adapter = adapter
        self._store = store if store is not None else NodeStore(config.database_path)
        self._policy = LocalPolicy(
            share_enabled=config.share_enabled,
            max_concurrency=config.max_concurrency,
            max_local_reserved_milli_credits=config.max_local_reserved_milli_credits,
        )
        self._device_key = load_private_key(config.device_private_key_b64)
        self._state = _State()
        self._seq: dict[str, int] = {}
        self._flushed: dict[str, int] = {}
        self._open_attempt_index: dict[str, tuple[str, int]] = {}
        self._hello_open_attempts: tuple[str, ...] = ()
        self._upstream_calls = 0
        self._in_flight = 0
        self._stop_event = threading.Event()
        self._connection: ClientConnection | None = None
        self._last_rx = 0.0
        #: The node config file whose share switch is polled, or `None` when the
        #: node was configured programmatically.
        self._policy_path = config.config_path
        self._policy_poll_interval_s = config.policy_poll_interval_s
        self._last_policy_poll = 0.0
        #: Whether the center has spoken on this session. The spool is not
        #: drained before HELLO.ACK, so a `resume` directive governs what is
        #: replayed instead of the node pushing events the center is about to
        #: ask it to abandon.
        self._link_ready = False
        # Startup recovery runs exactly once, before any connection exists. A
        # process that died mid-call may or may not have consumed upstream
        # tokens, so those executions become UNKNOWN and are never counted as
        # zero and never silently re-executed.
        interrupted = self._store.mark_interrupted_as_unknown(at=_now())
        if interrupted:
            logger.warning(
                "node startup: %d interrupted execution(s) marked UNKNOWN: %s",
                len(interrupted),
                ", ".join(interrupted),
            )

    # ------------------------------------------------------------------ #
    # Public read-only surface
    # ------------------------------------------------------------------ #

    @property
    def store(self) -> NodeStore:
        """Return the node's durable store."""
        return self._store

    @property
    def policy(self) -> LocalPolicy:
        """Return the local policy that makes this node's own limits final."""
        return self._policy

    @property
    def share_enabled(self) -> bool:
        """Return whether this node is currently accepting new offers.

        Read by `node.hello` and `node.heartbeat`, and consulted by `_admit`, so
        a paused node both advertises the pause (the center stops offering) and
        refuses any offer that raced the advertisement.
        """
        with self._state.lock:
            return self._policy.share_enabled

    @property
    def upstream_call_count(self) -> int:
        """Return successful upstream calls made by this connector's adapter.

        Counted only for completions the adapter actually returned, so a link
        drop or a policy refusal cannot be mistaken for a call, and a refused or
        duplicate attempt provably adds nothing here.
        """
        with self._state.lock:
            return self._upstream_calls

    # ------------------------------------------------------------------ #
    # Message handling (socket-free)
    # ------------------------------------------------------------------ #

    def handle_message(self, envelope: Envelope) -> list[Envelope]:
        """Handle one inbound message and return the messages to send.

        Pure with respect to the socket: it reads and writes the node store and
        may start a worker thread, but it never touches a connection.

        Args:
            envelope: The decoded inbound message.

        Returns:
            The envelopes the caller must send, in order.

        Raises:
            ProtocolError: If the payload is malformed for its type.
        """
        payload = envelope.payload
        if envelope.type == "hello.ack":
            return self._on_hello_ack(HelloAck.from_payload(payload))
        if envelope.type == "attempt.offer":
            return self._on_offer(AttemptOffer.from_payload(payload))
        if envelope.type == "attempt.execute":
            return self._on_execute(AttemptRef.from_payload(payload))
        if envelope.type == "attempt.cancel":
            self._on_cancel(AttemptRef.from_payload(payload))
            return []
        if envelope.type == "attempt.query":
            reference = self._ref_from_query(payload)
            record = self._lookup_execution(payload)
            return [self._state_envelope(reference, record)]
        if envelope.type == "event.ack":
            self._on_ack(EventAck.from_payload(payload))
            return []
        if envelope.type == "protocol.error":
            body = ProtocolErrorBody.from_payload(payload)
            logger.warning("center reported protocol error %s: %s", body.code, body.detail)
            return []
        return [
            error_envelope(
                "unexpected_message",
                f"{envelope.type} is not a message a center sends to a node",
                reply_to=envelope.msg_id,
            )
        ]

    # ------------------------------------------------------------------ #
    # Session
    # ------------------------------------------------------------------ #

    def run_once(self, *, connect_timeout: float | None = None) -> None:
        """Connect, run one session until the peer closes, and return.

        Used both by tests and by the reconnect loop. A `WebSocketClosed` — the
        peer closing, whether cleanly or mid-frame — is a normal end of session,
        not an error: the next `run_once` resumes from the spool.

        Args:
            connect_timeout: Overrides `config.connect_timeout_s` when given.

        Raises:
            WebSocketError: If the center cannot be reached or the handshake
                fails. The message never contains the platform credential.
        """
        timeout = (
            self._config.connect_timeout_s if connect_timeout is None else connect_timeout
        )
        headers = {"Authorization": f"Bearer {self._config.token}"}
        connection = ClientConnection.connect(
            self._config.center_url, headers=headers, timeout=timeout
        )
        with self._state.lock:
            self._connection = connection
        try:
            self._run_session(connection)
        except WebSocketClosed:
            logger.info("node link closed by the center; a reconnect will resume from the spool")
        finally:
            with self._state.lock:
                self._connection = None
            connection.close()

    def run_forever(self, *, stop_event: threading.Event | None = None) -> None:
        """Reconnect with bounded backoff until `stop_event` is set.

        Each failed session is logged in redacted form and the loop waits on the
        stop event rather than sleeping, so `stop` returns promptly.

        Args:
            stop_event: The event to watch. When omitted, the connector's own
                event is used, which `stop` sets.
        """
        if stop_event is not None:
            self._stop_event = stop_event
        backoff = self._config.reconnect_backoff_s
        if not backoff:
            raise ValueError("reconnect_backoff_s must not be empty")
        attempt_index = 0
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except WebSocketClosed:
                pass
            except Exception as exc:
                # A failed session must never kill the node: log it redacted and
                # back off, then reconnect.
                logger.warning(
                    "node link session failed (%s): %s",
                    type(exc).__name__,
                    _scrub(str(exc), self._config.token),
                )
            if self._stop_event.is_set():
                return
            delay = backoff[min(attempt_index, len(backoff) - 1)]
            attempt_index += 1
            self._stop_event.wait(delay)

    def stop(self) -> None:
        """Ask `run_forever` to return and wake a blocked session.

        Closing the live connection unblocks a reader parked in `recv_text`, so
        a session mid-wait ends immediately instead of waiting for its deadline.
        """
        self._stop_event.set()
        with self._state.lock:
            connection = self._connection
        if connection is not None:
            connection.close()

    # ------------------------------------------------------------------ #
    # Share switch (pause / resume without a restart)
    # ------------------------------------------------------------------ #

    def refresh_policy(self, *, force: bool = False) -> bool:
        """Re-read the share switch from the node's config file, if it has one.

        Why a polled file rather than a signal: a signal handler can only be
        installed from the main thread, which rules out a connector a host
        application runs on a worker thread; and the config file is something
        the operator already owns, so ``potluck share pause`` needs no pid file,
        no separate control channel, and survives a node restart. The cost is
        that the change takes effect within `policy_poll_interval_s` — a pause
        is not instantaneous, but it is bounded and short.

        A file that cannot be read, or that does not carry a boolean
        ``share_enabled``, is ignored: the last good policy stays in force. That
        matters because an editor that truncates and rewrites the file would
        otherwise be observed mid-write, and a transient parse failure silently
        pausing (or unpausing) a node is exactly the kind of surprise this
        switch must not have.

        Args:
            force: Skip the poll-interval throttle, for the first check of a
                session.

        Returns:
            True when the effective policy changed on this call.
        """
        path = self._policy_path
        if path is None:
            return False
        now = time.monotonic()
        if not force and now - self._last_policy_poll < self._policy_poll_interval_s:
            return False
        self._last_policy_poll = now
        try:
            raw = Path(path).read_text(encoding="utf-8")
            document = loads_strict(raw)
        except (OSError, PotluckError):
            return False
        if not isinstance(document, dict):
            return False
        share_enabled = document.get("share_enabled")
        if not isinstance(share_enabled, bool):
            return False
        max_concurrency = document.get("max_concurrency")
        max_reserved = document.get("max_local_reserved_milli_credits")
        changed = False
        with self._state.lock:
            if self._policy.share_enabled != share_enabled:
                self._policy.share_enabled = share_enabled
                changed = True
            if (
                isinstance(max_concurrency, int)
                and not isinstance(max_concurrency, bool)
                and max_concurrency >= 1
                and self._policy.max_concurrency != max_concurrency
            ):
                self._policy.max_concurrency = max_concurrency
                changed = True
            if (
                isinstance(max_reserved, int)
                and not isinstance(max_reserved, bool)
                and max_reserved >= 0
                and self._policy.max_local_reserved_milli_credits != max_reserved
            ):
                self._policy.max_local_reserved_milli_credits = max_reserved
                changed = True
        if changed:
            logger.info(
                "share policy reloaded from %s: share_enabled=%s",
                path,
                share_enabled,
            )
        return changed

    # ------------------------------------------------------------------ #
    # Inbound message handlers
    # ------------------------------------------------------------------ #

    def _on_hello_ack(self, ack: HelloAck) -> list[Envelope]:
        """Apply resume directives and report attempts that never started."""
        outgoing: list[Envelope] = []
        for directive in ack.resume:
            outgoing.extend(self._apply_directive(directive))
        with self._state.lock:
            open_attempts = self._hello_open_attempts
            index = dict(self._open_attempt_index)
        for attempt_id in open_attempts:
            if self._store.get_execution(attempt_id) is not None:
                continue
            # No execution row at all: the offer never started on this node. Say
            # so plainly so the center can re-offer instead of billing it.
            request_id, epoch = index[attempt_id]
            outgoing.append(
                self._state_envelope(
                    AttemptRef(request_id, attempt_id, epoch), None
                )
            )
        return outgoing

    def _apply_directive(self, directive: ResumeDirective) -> list[Envelope]:
        """Apply one resume directive, returning any messages to send."""
        attempt_id = directive.attempt_id
        if directive.action == "cancel":
            self._abandon_attempt(attempt_id)
            return []
        if directive.action == "abandon":
            self._abandon_attempt(attempt_id)
            return []
        # resend_spool: never re-execute. Resend the unacked spool, then report
        # the real local state so the center decides what the attempt is worth.
        reference = AttemptRef(directive.request_id, attempt_id, directive.epoch)
        outgoing = self._resend_envelopes(attempt_id)
        outgoing.append(self._state_envelope(reference, self._store.get_execution(attempt_id)))
        return outgoing

    def _on_offer(self, offer: AttemptOffer) -> list[Envelope]:
        """Validate the permit, consult local policy, then accept or reject."""
        reference = AttemptRef(offer.request_id, offer.attempt_id, offer.epoch)
        expected_permit = compute_permit_hash(
            request_id=offer.request_id,
            attempt_id=offer.attempt_id,
            epoch=offer.epoch,
            model=offer.model,
            request_digest=offer.request_digest,
            max_milli_credits=offer.max_milli_credits,
            authorization_hash=offer.authorization_hash,
        )
        if expected_permit != offer.permit_hash:
            logger.warning(
                "refusing offer for attempt %s: permit hash mismatch", offer.attempt_id
            )
            return [self._reject(reference, "offer terms do not match the signed permit")]
        with self._state.lock:
            existing = self._state.accepted.get(offer.attempt_id)
        if existing is not None:
            # Idempotent re-offer: echo the acceptance this node already made, so
            # the acceptance hash the report will carry stays the one committed.
            return [self._accept_envelope(existing)]
        admitted, reason = self._admit(offer.max_milli_credits)
        if not admitted:
            logger.info("refusing offer for attempt %s: %s", offer.attempt_id, reason)
            return [self._reject(reference, reason)]
        with self._state.lock:
            live = len(self._state.workers)
        if live >= self._config.max_concurrent_executions:
            return [
                self._reject(
                    reference,
                    f"execution slot limit reached ({live}/"
                    f"{self._config.max_concurrent_executions})",
                )
            ]
        accepted_at = _now()
        acceptance_hash = compute_acceptance_hash(
            permit_hash=offer.permit_hash,
            request_id=offer.request_id,
            attempt_id=offer.attempt_id,
            epoch=offer.epoch,
            adapter_name=self._config.adapter_name,
            accepted_at=accepted_at,
        )
        accepted = _AcceptedOffer(offer=offer, acceptance_hash=acceptance_hash)
        with self._state.lock:
            self._state.accepted[offer.attempt_id] = accepted
            self._state.tracked.add(offer.attempt_id)
        return [self._accept_envelope(accepted)]

    def _on_execute(self, reference: AttemptRef) -> list[Envelope]:
        """Start one execution, exactly once per attempt.

        The deduplication key is the local execution table, not memory: a
        duplicate EXECUTE for an attempt this node already began returns the
        spool and the real state instead of a second upstream call. That is the
        guarantee that a reconnect or a re-dispatched message cannot
        double-consume the operator's upstream quota.
        """
        attempt_id = reference.attempt_id
        with self._state.lock:
            accepted = self._state.accepted.get(attempt_id)
        if accepted is None:
            return [
                error_envelope(
                    "unknown_attempt",
                    f"attempt {attempt_id} was never accepted by this node",
                )
            ]
        offer = accepted.offer
        record, is_new = self._store.begin_execution(
            attempt_id=attempt_id,
            request_id=offer.request_id,
            epoch=offer.epoch,
            request_digest=offer.request_digest,
            at=_now(),
        )
        if not is_new:
            # Already started. Re-drain any unacked spool; with nothing spooled,
            # report what actually happened from the durable row.
            resend = self._resend_envelopes(attempt_id)
            if resend:
                return resend
            return [self._state_envelope(reference, record)]
        with self._state.lock:
            cancel_event = self._state.cancel_events.get(attempt_id)
        if cancel_event is not None and cancel_event.is_set():
            self._store.finish_execution(attempt_id, "FAILED", at=_now())
            return []
        try:
            self._begin(offer.max_milli_credits)
        except StateTransitionError as exc:
            self._store.finish_execution(attempt_id, "FAILED", at=_now())
            self._enqueue_failure(attempt_id, reference, f"local policy refused at execute: {exc}")
            return []
        if cancel_event is None:
            cancel_event = threading.Event()
            with self._state.lock:
                self._state.cancel_events[attempt_id] = cancel_event
        with self._state.lock:
            self._in_flight += 1
        worker = threading.Thread(
            target=self._run_execution,
            args=(accepted, cancel_event),
            name=f"potluck-exec-{attempt_id}",
            daemon=True,
        )
        with self._state.lock:
            self._state.workers[attempt_id] = worker
            self._state.tracked.add(attempt_id)
        try:
            worker.start()
        except RuntimeError:
            # The thread could not be created: release the reservation rather
            # than leak local concurrency, then surface the failure on the wire.
            with self._state.lock:
                self._state.workers.pop(attempt_id, None)
                self._in_flight = max(0, self._in_flight - 1)
            self._end(offer.max_milli_credits)
            self._store.finish_execution(attempt_id, "FAILED", at=_now())
            self._enqueue_failure(attempt_id, reference, "local worker thread could not start")
        return []

    def _on_cancel(self, reference: AttemptRef) -> None:
        """Signal a worker to stop, mark the execution, and drop its spool."""
        attempt_id = reference.attempt_id
        with self._state.lock:
            event = self._state.cancel_events.get(attempt_id)
            if event is None:
                event = threading.Event()
                self._state.cancel_events[attempt_id] = event
        event.set()
        record = self._store.get_execution(attempt_id)
        if record is not None and record.finished_at is None:
            self._store.finish_execution(attempt_id, "FAILED", at=_now())
        self._drop_spool(attempt_id)

    def _on_ack(self, ack: EventAck) -> None:
        """Retire the acknowledged contiguous prefix of an attempt's spool.

        The cursor only moves forward inside `ack_through`, so a late or
        duplicate ACK cannot re-open work that was already retired.
        """
        with self._state.lock:
            self._state.tracked.add(ack.attempt_id)
        self._store.ack_through(ack.attempt_id, ack.acked_seq, at=_now())

    def _admit(self, milli_credits: int) -> tuple[bool, str]:
        """Consult local policy under the connector lock.

        `LocalPolicy` keeps plain integer counters and does not synchronize them
        itself, while the connector releases a reservation from a worker thread.
        Every policy call is therefore serialized on this connector's lock so a
        concurrent `begin` and `end` cannot lose an update.
        """
        with self._state.lock:
            return self._policy.admit(milli_credits)

    def _begin(self, milli_credits: int) -> None:
        """Reserve local capacity under the connector lock.

        Raises:
            StateTransitionError: If the attempt was not admissible, which means
                the local limits changed between OFFER and EXECUTE.
        """
        with self._state.lock:
            self._policy.begin(milli_credits)

    def _end(self, milli_credits: int) -> None:
        """Release local capacity under the connector lock.

        Raises:
            StateTransitionError: If no attempt is in flight.
        """
        with self._state.lock:
            self._policy.end(milli_credits)

    # ------------------------------------------------------------------ #
    # Execution worker (never touches the socket)
    # ------------------------------------------------------------------ #

    def _run_execution(
        self, accepted: _AcceptedOffer, cancel_event: threading.Event
    ) -> None:
        """Execute one attempt, spooling every event for the socket loop.

        Runs on a worker thread. It writes only to the node store; the socket
        loop is the sole sender. Local concurrency is always released, even when
        the adapter fails, so a failed execution cannot leak a reservation.

        Which of the two paths runs is the only thing that differs between a
        streamed and a buffered attempt; both end by spooling the same usage
        report and completion, so the center settles them identically.
        """
        offer = accepted.offer
        attempt_id = offer.attempt_id
        reference = AttemptRef(offer.request_id, attempt_id, offer.epoch)
        try:
            if self._streaming_available(offer):
                self._execute_streaming(accepted, reference, cancel_event)
            else:
                self._execute_buffered(accepted, reference, cancel_event)
        finally:
            with self._state.lock:
                self._state.workers.pop(attempt_id, None)
                self._in_flight = max(0, self._in_flight - 1)
            try:
                self._end(offer.max_milli_credits)
            except StateTransitionError:
                logger.warning(
                    "attempt %s ended with no local reservation to release", attempt_id
                )

    def _streaming_available(self, offer: AttemptOffer) -> bool:
        """Whether this attempt should run on the adapter's streaming path.

        Streaming is taken only when the center asked for `stream_strict` *and*
        the adapter reports it can deliver increments. An adapter without that
        capability falls back to the buffered path rather than fabricating deltas
        that never arrived live.

        Args:
            offer: The accepted offer whose delivery mode decides.

        Returns:
            True when the streaming path should be used.
        """
        if offer.delivery_mode != "stream_strict":
            return False
        return bool(getattr(self._adapter, "supports_streaming", False))

    def _execute_buffered(
        self, accepted: _AcceptedOffer, reference: AttemptRef, cancel_event: threading.Event
    ) -> None:
        """Complete in one piece, then split the answer into transport-sized deltas.

        The original path: the adapter returns the whole completion and
        `chunk_text` slices it. A `stream_strict` request served by an adapter
        that cannot stream still gets its output published this way — just not
        incrementally.
        """
        offer = accepted.offer
        attempt_id = offer.attempt_id
        try:
            result = self._adapter.complete(
                offer.messages,
                request_digest=offer.request_digest,
                parameters=offer.parameters,
            )
        except AdapterQuotaExceeded as exc:
            self._store.finish_execution(attempt_id, "FAILED", at=_now())
            self._enqueue_failure(
                attempt_id,
                reference,
                f"upstream quota exhausted; retrying would not help: {exc}",
            )
            return
        except AdapterOutcomeUnknown as exc:
            self._fail_with_unknown_outcome(attempt_id, exc)
            return
        except AdapterError as exc:
            self._enqueue_upstream_failure(attempt_id, reference, exc)
            return
        with self._state.lock:
            self._upstream_calls += 1
        self._store.record_upstream_call(attempt_id)
        if cancel_event.is_set():
            return
        for chunk in chunk_text(result.text, self._config.delta_chars):
            if cancel_event.is_set():
                return
            self._enqueue_output_delta(attempt_id, reference, chunk)
        if cancel_event.is_set():
            return
        self._finish_success(accepted, reference, result.usage, cancel_event)

    def _execute_streaming(
        self, accepted: _AcceptedOffer, reference: AttemptRef, cancel_event: threading.Event
    ) -> None:
        """Publish each upstream increment the moment it is read.

        Same spool discipline as the buffered path — the worker writes only spool
        rows and the socket loop stays the sole sender — but a chunk is spooled as
        it arrives, so the center can commit it while the model is still writing.
        The execution row is left SUBMITTING until the stream ends, so a process
        killed mid-stream restarts as UNKNOWN instead of as a fabricated success.
        A stream that breaks early is reported as a failure, never as a short but
        complete answer.

        Args:
            accepted: The accepted offer being executed.
            reference: The attempt reference for spooled events.
            cancel_event: Set by CANCEL; checked between chunks.
        """
        offer = accepted.offer
        attempt_id = offer.attempt_id
        usage: NormalizedUsage | None = None
        emitted = False
        chunks: Iterator[StreamChunk] | None = None
        try:
            try:
                # The call itself is inside the try: an adapter that fails before
                # returning an iterator must fail the attempt, not escape the
                # worker thread and leave the row SUBMITTING until the deadline.
                chunks = self._adapter.stream(
                    offer.messages,
                    request_digest=offer.request_digest,
                    parameters=offer.parameters,
                )
                for chunk in chunks:
                    if cancel_event.is_set():
                        return
                    if chunk.usage is not None:
                        # Usage is only ever carried by the terminal chunk; keep
                        # the last one seen so a re-reported total wins.
                        usage = chunk.usage
                    if not chunk.text:
                        continue
                    self._enqueue_output_delta(attempt_id, reference, chunk.text)
                    emitted = True
                if not emitted:
                    raise AdapterError("upstream stream produced no output")
            except AdapterQuotaExceeded as exc:
                self._store.finish_execution(attempt_id, "FAILED", at=_now())
                self._enqueue_failure(
                    attempt_id,
                    reference,
                    f"upstream quota exhausted; retrying would not help: {exc}",
                )
                return
            except AdapterOutcomeUnknown as exc:
                self._fail_with_unknown_outcome(attempt_id, exc)
                return
            except AdapterError as exc:
                self._enqueue_upstream_failure(attempt_id, reference, exc)
                return
        finally:
            # Close the generator so an early return (CANCEL) releases the
            # upstream connection instead of leaving it to the garbage collector.
            close = getattr(chunks, "close", None)
            if callable(close):
                close()
        with self._state.lock:
            self._upstream_calls += 1
        self._store.record_upstream_call(attempt_id)
        if cancel_event.is_set():
            return
        self._finish_success(accepted, reference, usage, cancel_event)

    def _fail_with_unknown_outcome(self, attempt_id: str, exc: AdapterOutcomeUnknown) -> None:
        """Record a dispatched-but-unanswered call without claiming either outcome.

        The adapter guarantees the request bytes were sent, so the submission is
        a recorded fact (`record_upstream_call`); whether the upstream executed
        it is not, so the execution row ends UNKNOWN rather than FAILED. No
        failure event is spooled: an `attempt.failed` report reads as a clean
        failure and would let the center open a replacement attempt while the
        original call may still be running upstream — the double-execution this
        method exists to prevent. The center's tick probe asks for the attempt
        state instead, and its reconciliation turns `certainty="unknown"` into
        OUTCOME_UNKNOWN: usage stays unknown, never zero, nobody is billed, and
        no second upstream call is made.
        """
        with self._state.lock:
            self._upstream_calls += 1
        self._store.record_upstream_call(attempt_id)
        self._store.finish_execution(attempt_id, "UNKNOWN", at=_now())

    def _enqueue_upstream_failure(
        self, attempt_id: str, reference: AttemptRef, exc: AdapterError
    ) -> None:
        """Fail the execution, naming whether output had already been published.

        Read from the store rather than from this run's memory so the wording
        survives a restart: an acknowledged cursor or a still-spooled event means
        output exists and the failure must not claim it happened "before any
        output".
        """
        emitted = self._has_emitted_output(attempt_id)
        detail = (
            f"upstream failed after output had already been emitted: {exc}"
            if emitted
            else f"upstream call failed before any output: {exc}"
        )
        self._store.finish_execution(attempt_id, "FAILED", at=_now())
        self._enqueue_failure(attempt_id, reference, detail)

    def _enqueue_output_delta(
        self, attempt_id: str, reference: AttemptRef, text: str
    ) -> None:
        """Spool one `output.delta` under the next per-attempt sequence number."""
        seq = self._next_seq(attempt_id)
        self._enqueue(
            attempt_id,
            seq,
            "output.delta",
            AttemptEvent(
                ref=reference, seq=seq, event_type="output.delta", text=text
            ).to_payload(),
        )

    def _finish_success(
        self,
        accepted: _AcceptedOffer,
        reference: AttemptRef,
        usage: NormalizedUsage | None,
        cancel_event: threading.Event,
    ) -> None:
        """Spool the signed usage report and completion, then retire the row.

        Shared by both execution paths so a streamed and a buffered answer settle
        by the same rule: one measurement, one report, the same terminal events.
        A missing usage is reported as `unknown`, never as zero.

        Args:
            accepted: The accepted offer, carrying the permit and acceptance hashes.
            reference: The attempt reference for spooled events.
            usage: The usage the adapter reported, or `None` when it reported none.
            cancel_event: Set by CANCEL; a cancel before the terminal events stops
                the report from being written.
        """
        offer = accepted.offer
        attempt_id = offer.attempt_id
        resolved = usage or NormalizedUsage(
            input_total=0, output_total=0, completeness="unknown"
        )
        measurement = Measurement(
            measurement_id=f"m-{attempt_id}",
            scope="upstream_call",
            semantics="terminal_total",
            counter_epoch=f"epoch-{offer.epoch}",
            revision=1,
            usage=resolved,
            attempt_id=attempt_id,
        )
        usage_seq = self._next_seq(attempt_id)
        report = build_usage_report(
            report_id=f"rep-{attempt_id}",
            permit_hash=offer.permit_hash,
            acceptance_hash=accepted.acceptance_hash,
            attempt_id=attempt_id,
            provider_id=self._config.provider_id,
            device_id=self._config.device_id,
            measurement=measurement,
            outcome="completed",
            device_private_key=self._device_key,
            device_key_id=self._config.device_key_id,
            reported_at=_now(),
            final_event_seq=usage_seq,
        )
        self._enqueue(
            attempt_id,
            usage_seq,
            "usage.final",
            AttemptEvent(
                ref=reference,
                seq=usage_seq,
                event_type="usage.final",
                usage=resolved,
                report=_signed_record_to_dict(report),
            ).to_payload(),
        )
        if cancel_event.is_set():
            return
        completed_seq = self._next_seq(attempt_id)
        self._enqueue(
            attempt_id,
            completed_seq,
            "attempt.completed",
            AttemptEvent(
                ref=reference,
                seq=completed_seq,
                event_type="attempt.completed",
                outcome="completed",
                final=True,
            ).to_payload(),
        )
        self._store.finish_execution(attempt_id, "SUBMITTED", at=_now())

    def _enqueue_failure(self, attempt_id: str, reference: AttemptRef, detail: str) -> None:
        """Spool a terminal `attempt.failed` naming why the attempt failed."""
        seq = self._next_seq(attempt_id)
        self._enqueue(
            attempt_id,
            seq,
            "attempt.failed",
            AttemptEvent(
                ref=reference,
                seq=seq,
                event_type="attempt.failed",
                outcome="failed",
                detail=detail,
                final=True,
            ).to_payload(),
        )

    def _enqueue(
        self, attempt_id: str, seq: int, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Write one event into the durable spool for the socket loop to send."""
        self._store.enqueue_spool(
            attempt_id=attempt_id,
            attempt_seq=seq,
            event_type=event_type,
            payload=payload,
            at=_now(),
        )
        with self._state.lock:
            self._state.tracked.add(attempt_id)

    def _next_seq(self, attempt_id: str) -> int:
        """Return the next strictly increasing per-attempt sequence number."""
        with self._state.lock:
            seq = self._seq.get(attempt_id, 0) + 1
            self._seq[attempt_id] = seq
            return seq

    def _has_emitted_output(self, attempt_id: str) -> bool:
        """Whether this attempt already put an event on the wire or the spool.

        Read from the store rather than memory so the answer survives a restart:
        an acknowledged cursor above zero, or any still-spooled event, means
        output exists and a later failure must not be described as "before any
        output".
        """
        if self._store.ack_cursor(attempt_id) > 0:
            return True
        return bool(self._store.unacked_spool(attempt_id))

    # ------------------------------------------------------------------ #
    # Session loop (the only sender)
    # ------------------------------------------------------------------ #

    def _run_session(self, connection: ClientConnection) -> None:
        """Send HELLO, then pump the link until it closes or `stop` is set."""
        self._flushed.clear()
        self._link_ready = False
        self.refresh_policy(force=True)
        hello, open_index = self._build_hello()
        with self._state.lock:
            self._hello_open_attempts = tuple(sorted(open_index))
            self._open_attempt_index = open_index
        connection.send_text(hello.encode())
        self._last_rx = time.monotonic()
        while not self._stop_event.is_set():
            if self._link_ready:
                if self.refresh_policy():
                    # A pause/resume must reach the center promptly rather than
                    # waiting out the heartbeat interval: the heartbeat is what
                    # updates the node's advertised `share_enabled`, and until
                    # it lands the center may still pick this node for new work.
                    connection.send_text(self._heartbeat_envelope().encode())
                    self._last_rx = time.monotonic()
                self._flush_spool(connection)
                if time.monotonic() - self._last_rx >= self._config.heartbeat_interval_s:
                    connection.send_text(self._heartbeat_envelope().encode())
                    self._last_rx = time.monotonic()
            try:
                text = connection.recv_text(timeout=_POLL_INTERVAL_S)
            except WebSocketTimeout:
                continue
            self._last_rx = time.monotonic()
            self._dispatch(connection, text)

    def _dispatch(self, connection: ClientConnection, text: str) -> None:
        """Decode one message, handle it, and send whatever it produced."""
        try:
            envelope = Envelope.decode(text)
        except ProtocolError as exc:
            self._send_protocol_error(connection, "bad_message", str(exc), None)
            return
        self._link_ready = True
        try:
            outgoing = self.handle_message(envelope)
        except ProtocolError as exc:
            self._send_protocol_error(connection, "invalid_payload", str(exc), envelope.msg_id)
            return
        for out in outgoing:
            connection.send_text(out.encode())

    def _send_protocol_error(
        self,
        connection: ClientConnection,
        code: str,
        detail: str,
        reply_to: str | None,
    ) -> None:
        """Tell the peer its message was rejected, and keep the session alive."""
        logger.warning("rejecting inbound message (%s): %s", code, detail)
        connection.send_text(error_envelope(code, detail, reply_to=reply_to).encode())

    def _flush_spool(self, connection: ClientConnection) -> None:
        """Send every not-yet-sent unacked spool entry, in sequence order.

        Only the socket loop calls this, so the link has exactly one ordered
        writer. Events already sent in this session are not re-sent; a fresh
        session (or an explicit resend) clears that high-water mark and the
        store's unacked rows are sent again. Retirement happens only through
        `store.ack_through` on an `event.ack`.
        """
        with self._state.lock:
            attempt_ids = sorted(self._state.tracked)
        drained: list[str] = []
        for attempt_id in attempt_ids:
            sent_through = self._flushed.get(attempt_id, 0)
            pending = False
            for record in self._store.unacked_spool(attempt_id):
                if record.attempt_seq <= sent_through:
                    # Sent but not yet acknowledged: keep it tracked so a future
                    # flush can retransmit it if the cursor never catches up.
                    pending = True
                    continue
                connection.send_text(self._event_envelope(record).encode())
                self._flushed[attempt_id] = record.attempt_seq
                pending = True
            if not pending:
                drained.append(attempt_id)
        if drained:
            # Forgetting a fully drained, non-running attempt keeps the tracked
            # set bounded on a node that runs for weeks. A worker that spools a
            # new event adds the attempt back, so this can never lose an event.
            with self._state.lock:
                for attempt_id in drained:
                    if attempt_id in self._state.workers:
                        continue
                    self._state.tracked.discard(attempt_id)
                    self._state.cancel_events.pop(attempt_id, None)

    def _resend_envelopes(self, attempt_id: str) -> list[Envelope]:
        """Force a resend of every unacked spool entry for one attempt.

        Used by the `resend_spool` directive and by a duplicate EXECUTE. The
        entries are re-sent regardless of the session's high-water mark, because
        the caller has explicitly asked for the spool to be replayed; the
        high-water mark is then advanced so the periodic flush does not send
        them a second time.
        """
        records = sorted(self._store.unacked_spool(attempt_id), key=_spool_order)
        if records:
            self._flushed[attempt_id] = max(
                self._flushed.get(attempt_id, 0), records[-1].attempt_seq
            )
        return [self._event_envelope(record) for record in records]

    def _drop_spool(self, attempt_id: str) -> None:
        """Delete an attempt's spool rows through the ack cursor.

        `ack_through` is the store's only deletion primitive and it also advances
        the durable cursor, so a later reconnect will not replay events the node
        has deliberately abandoned.
        """
        records = self._store.unacked_spool(attempt_id)
        if not records:
            return
        highest = max(record.attempt_seq for record in records)
        self._store.ack_through(attempt_id, highest, at=_now())
        with self._state.lock:
            self._flushed[attempt_id] = max(self._flushed.get(attempt_id, 0), highest)

    def _abandon_attempt(self, attempt_id: str) -> None:
        """Stop work on an attempt, mark it failed, and drop its spool silently.

        Used for both `cancel` and `abandon` directives. Nothing is sent for the
        attempt: the center asked to end it, so the node does not manufacture a
        terminal event for it.
        """
        with self._state.lock:
            event = self._state.cancel_events.get(attempt_id)
            if event is None:
                event = threading.Event()
                self._state.cancel_events[attempt_id] = event
        event.set()
        record = self._store.get_execution(attempt_id)
        if record is not None and record.finished_at is None:
            self._store.finish_execution(attempt_id, "FAILED", at=_now())
        self._drop_spool(attempt_id)

    # ------------------------------------------------------------------ #
    # Envelope construction
    # ------------------------------------------------------------------ #

    def _build_hello(self) -> tuple[Envelope, dict[str, tuple[str, int]]]:
        """Build `node.hello` and the index of every attempt it advertises.

        `open_attempts` is the union of unfinished executions and offers this
        node accepted but has not yet executed. The index records the request
        and epoch of each, so an attempt with no execution row can still be
        reported to the center with a complete reference.
        """
        records = self._store.open_executions()
        index: dict[str, tuple[str, int]] = {
            record.attempt_id: (record.request_id, record.epoch) for record in records
        }
        with self._state.lock:
            accepted = dict(self._state.accepted)
            share_enabled = self._policy.share_enabled
            max_concurrency = self._policy.max_concurrency
            max_reserved = self._policy.max_local_reserved_milli_credits
        for attempt_id, item in accepted.items():
            index.setdefault(attempt_id, (item.offer.request_id, item.offer.epoch))
        hello = NodeHello(
            node_id=self._config.node_id,
            device_id=self._config.device_id,
            device_key_id=self._config.device_key_id,
            provider_id=self._config.provider_id,
            adapter_name=self._config.adapter_name,
            model=self._config.model,
            policy=NodePolicy(
                share_enabled=share_enabled,
                max_concurrency=max_concurrency,
                max_local_reserved_milli_credits=max_reserved,
                policy_version=self._policy.policy_version,
            ),
            open_attempts=tuple(sorted(index)),
        )
        return Envelope(type="node.hello", payload=hello.to_payload()), index

    def _heartbeat_envelope(self) -> Envelope:
        """Build `node.heartbeat`, the application-level liveness signal."""
        with self._state.lock:
            in_flight = self._in_flight
        heartbeat = NodeHeartbeat(
            in_flight=in_flight,
            share_enabled=self._policy.share_enabled,
            at=_now(),
        )
        return Envelope(type="node.heartbeat", payload=heartbeat.to_payload())

    def _accept_envelope(self, accepted: _AcceptedOffer) -> Envelope:
        """Build the `attempt.accept` for an accepted offer."""
        offer = accepted.offer
        decision = AttemptDecision(
            ref=AttemptRef(offer.request_id, offer.attempt_id, offer.epoch),
            accepted=True,
            permit_hash=offer.permit_hash,
            acceptance_hash=accepted.acceptance_hash,
        )
        return Envelope(type="attempt.accept", payload=decision.to_payload())

    def _reject(self, reference: AttemptRef, reason: str) -> Envelope:
        """Build the `attempt.reject` for a refused offer."""
        decision = AttemptDecision(ref=reference, accepted=False, reason=reason)
        return Envelope(type="attempt.reject", payload=decision.to_payload())

    def _state_envelope(
        self, reference: AttemptRef, record: ExecutionRecord | None
    ) -> Envelope:
        """Build an `attempt.state` describing what this node actually did.

        A missing execution row is reported as FAILED/not_submitted with zero
        upstream calls — the offer never started, so the center may re-offer it.
        A present row reports its real state and never invents a zero: an
        UNKNOWN row stays UNKNOWN/unknown.
        """
        if record is None:
            state: ExecutionState = "FAILED"
            certainty: ExecutionCertainty = "not_submitted"
            upstream_calls = 0
        else:
            state = _execution_state(record.state)
            certainty = _certainty_for(record)
            upstream_calls = record.upstream_calls
        payload = AttemptState(
            ref=reference,
            state=state,
            certainty=certainty,
            upstream_calls=upstream_calls,
            last_seq=self._last_seq(reference.attempt_id),
        )
        return Envelope(type="attempt.state", payload=payload.to_payload())

    def _last_seq(self, attempt_id: str) -> int:
        """Return the highest sequence this attempt has produced, acked or not."""
        highest = self._store.ack_cursor(attempt_id)
        for record in self._store.unacked_spool(attempt_id):
            highest = max(highest, record.attempt_seq)
        return highest

    def _lookup_execution(self, payload: dict[str, Any]) -> ExecutionRecord | None:
        """Return the execution row a query names, or `None`."""
        attempt_id = payload.get("attempt_id")
        if not isinstance(attempt_id, str):
            return None
        return self._store.get_execution(attempt_id)

    def _ref_from_query(self, payload: dict[str, Any]) -> AttemptRef:
        """Parse an `attempt.query` reference, filling epoch from the store.

        The center's query carries only `request_id` and `attempt_id`; the epoch
        is a local fact, so it is read from the execution row when the message
        does not carry one.

        Raises:
            ProtocolError: If the message names no request or attempt.
        """
        if "epoch" in payload:
            return AttemptRef.from_payload(payload)
        request_id = payload.get("request_id")
        attempt_id = payload.get("attempt_id")
        if not isinstance(request_id, str) or not request_id:
            raise ProtocolError("attempt.query requires request_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ProtocolError("attempt.query requires attempt_id")
        record = self._store.get_execution(attempt_id)
        epoch = record.epoch if record is not None else 0
        return AttemptRef(request_id, attempt_id, epoch)

    def _event_envelope(self, record: SpoolRecord) -> Envelope:
        """Wrap a spooled payload as the `attempt.event` the center expects."""
        return Envelope(type="attempt.event", payload=record.payload)


def _spool_order(record: SpoolRecord) -> int:
    """Sort key for spool records: their per-attempt sequence number."""
    return record.attempt_seq


def _execution_state(value: str) -> ExecutionState:
    """Validate a stored execution state string against the wire enum.

    Raises:
        PotluckError: If the store holds a state this transport cannot express,
            which would mean the store and the wire disagreed about the state
            machine.
    """
    if value not in _STATE_VALUES:
        raise PotluckError(f"stored execution state {value!r} is not a wire state")
    return cast("ExecutionState", value)


def _certainty_for(record: ExecutionRecord) -> ExecutionCertainty:
    """Classify what a stored execution proves about upstream submission.

    UNKNOWN means the node cannot say whether the upstream was called and must
    not be read as zero. A recorded upstream call is proof of submission; no
    call and no UNKNOWN state means the attempt never reached the upstream.
    """
    if record.state == "UNKNOWN":
        return "unknown"
    if record.upstream_calls > 0:
        return "submitted"
    return "not_submitted"


def _signed_record_to_dict(record: SignedRecord) -> dict[str, Any]:
    """Render a signed record as the mapping carried on the wire.

    The center rebuilds a `SignedRecord` from this and verifies the signature
    against the registry, so the payload and signature must travel verbatim; the
    remaining fields are the record's identity metadata.
    """
    return {
        "schema": record.schema,
        "record_id": record.record_id,
        "revision": record.revision,
        "signed_at": record.signed_at.isoformat(),
        "signer_key_id": record.signer_key_id,
        "signer_id": record.signer_id,
        "signature": record.signature,
        "previous_record_hash": record.previous_record_hash,
        "payload": dict(record.payload),
    }


def _scrub(text: str, token: str) -> str:
    """Replace every occurrence of the credential so a log line is safe."""
    if not token:
        return text
    return text.replace(token, _TOKEN_MASK)