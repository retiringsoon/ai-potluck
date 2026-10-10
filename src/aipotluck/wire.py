"""Wire protocol for the center <-> connector and consumer <-> center links.

Every message is a strictly parsed JSON envelope. Parsing goes through
`canonical.loads_strict`, so the network boundary inherits the same refusals as
the signing path: floats, duplicate keys, and `NaN`/`Infinity` are rejected on
arrival rather than reaching domain code.

An unknown message type is a protocol violation, never a silent ignore. A peer
that does not understand a message must be told, or it will believe the work was
accepted.

The transport is intentionally dumb: this module defines *what* is said, and
`aipotluck.websocket` defines how bytes move. Keeping the two apart means the
message set can be tested without a socket, and the framing can be tested
without the domain.
"""

from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from .canonical import canonical_digest, canonicalize, loads_strict
from .errors import ProtocolError
from .metering import NormalizedUsage

SCHEMA_ENVELOPE = "aipotluck.envelope.v1"

#: Largest accepted message on any link. An output delta larger than this is a
#: protocol violation: the sender must split it, not push it.
MAX_MESSAGE_BYTES = 256 * 1024

#: Longest output delta the center accepts in one event, in characters.
MAX_DELTA_CHARS = 64 * 1024

EventType = Literal["output.delta", "usage.final", "attempt.failed", "attempt.completed"]
DeliveryMode = Literal["stream_strict", "buffered_complete"]
ExecutionState = Literal["SUBMITTING", "SUBMITTED", "FAILED", "UNKNOWN"]
ExecutionCertainty = Literal["not_submitted", "submitted", "unknown"]

MESSAGE_TYPES = frozenset(
    {
        "node.hello",
        "hello.ack",
        "node.heartbeat",
        "attempt.offer",
        "attempt.accept",
        "attempt.reject",
        "attempt.execute",
        "attempt.cancel",
        "attempt.query",
        "attempt.state",
        "attempt.event",
        "event.ack",
        "protocol.error",
    }
)


def new_msg_id() -> str:
    """Return a fresh, unguessable message identifier."""
    return secrets.token_hex(12)


def _instant(value: dt.datetime) -> dt.datetime:
    """Return `value` as an aware UTC instant.

    Raises:
        ProtocolError: If the value is naive, which would make ordering
            ambiguous on the wire.
    """
    if value.tzinfo is None:
        raise ProtocolError("wire timestamps must be timezone-aware")
    return value.astimezone(dt.UTC)


def _parse_instant(raw: object, field_name: str) -> dt.datetime:
    """Parse an ISO 8601 instant from a payload field.

    Raises:
        ProtocolError: If the value is absent, not a string, or unparseable.
    """
    if not isinstance(raw, str):
        raise ProtocolError(f"{field_name} must be an ISO 8601 string, got {type(raw).__name__}")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ProtocolError(f"{field_name} is not a valid ISO 8601 instant: {raw!r}") from exc
    return _instant(parsed)


def _require_str(payload: dict[str, Any], key: str) -> str:
    """Return a required non-empty string field.

    Raises:
        ProtocolError: If the field is missing, empty, or not a string.
    """
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{key} must be a non-empty string")
    return value


def _require_int(payload: dict[str, Any], key: str, *, minimum: int = 0) -> int:
    """Return a required integer field.

    `bool` is rejected explicitly: `True` is an `int` in Python, and accepting it
    would let a peer send `true` where a count belongs.

    Raises:
        ProtocolError: If the field is missing, not an integer, or below
            `minimum`.
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{key} must be an integer")
    if value < minimum:
        raise ProtocolError(f"{key} must be >= {minimum}, got {value}")
    return value


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    """Return an optional string field, treating absence as `None`."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProtocolError(f"{key} must be a string when present")
    return value


@dataclass(frozen=True)
class Envelope:
    """A single wire message with its routing metadata."""

    type: str
    payload: dict[str, Any]
    msg_id: str = field(default_factory=new_msg_id)
    sent_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    reply_to: str | None = None
    schema: str = SCHEMA_ENVELOPE

    def encode(self) -> str:
        """Render the message as canonical JSON text.

        Raises:
            ProtocolError: If the payload contains a value that cannot be
                canonicalized, such as a float.
        """
        body = {
            "schema": self.schema,
            "type": self.type,
            "msg_id": self.msg_id,
            "sent_at": _instant(self.sent_at).isoformat(),
            "reply_to": self.reply_to,
            "payload": self.payload,
        }
        text = canonicalize(body).decode("utf-8")
        if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ProtocolError(
                f"{self.type} message is {len(text)} bytes, over the "
                f"{MAX_MESSAGE_BYTES}-byte limit"
            )
        return text

    def to_dict(self) -> dict[str, Any]:
        """Return the message as a plain mapping, for logging and tests."""
        return {
            "schema": self.schema,
            "type": self.type,
            "msg_id": self.msg_id,
            "sent_at": _instant(self.sent_at).isoformat(),
            "reply_to": self.reply_to,
            "payload": self.payload,
        }

    @classmethod
    def decode(cls, text: str | bytes) -> Envelope:
        """Parse and validate an envelope from wire bytes.

        Args:
            text: The raw message body.

        Returns:
            The parsed envelope.

        Raises:
            ProtocolError: If the body is not strict JSON, is not an object, has
                an unknown schema or message type, or carries no message ID.
        """
        try:
            body = loads_strict(text)
        except ProtocolError:
            raise
        except Exception as exc:
            raise ProtocolError(f"message body is not strict JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise ProtocolError("a wire message must be a JSON object")
        schema = body.get("schema")
        if schema != SCHEMA_ENVELOPE:
            raise ProtocolError(f"unsupported envelope schema {schema!r}")
        msg_type = body.get("type")
        if not isinstance(msg_type, str) or msg_type not in MESSAGE_TYPES:
            raise ProtocolError(f"unknown message type {msg_type!r}")
        payload = body.get("payload")
        if not isinstance(payload, dict):
            raise ProtocolError("message payload must be a JSON object")
        return cls(
            type=msg_type,
            payload=cast("dict[str, Any]", payload),
            msg_id=_require_str(body, "msg_id"),
            sent_at=_parse_instant(body.get("sent_at"), "sent_at"),
            reply_to=_optional_str(body, "reply_to"),
            schema=schema,
        )


@dataclass(frozen=True)
class NodePolicy:
    """Contribution-side limits advertised in `node.hello`.

    These are the node's own settings. The platform cannot raise them; it can
    only decide whether to dispatch at all.
    """

    share_enabled: bool
    max_concurrency: int
    max_local_reserved_milli_credits: int
    policy_version: int = 1
    paused_reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical payload."""
        return {
            "share_enabled": self.share_enabled,
            "max_concurrency": self.max_concurrency,
            "max_local_reserved_milli_credits": self.max_local_reserved_milli_credits,
            "policy_version": self.policy_version,
            "paused_reason": self.paused_reason,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> NodePolicy:
        """Parse the policy from a `node.hello` payload.

        Raises:
            ProtocolError: If a field is missing or has the wrong type.
        """
        share_enabled = payload.get("share_enabled")
        if not isinstance(share_enabled, bool):
            raise ProtocolError("share_enabled must be a boolean")
        return cls(
            share_enabled=share_enabled,
            max_concurrency=_require_int(payload, "max_concurrency", minimum=1),
            max_local_reserved_milli_credits=_require_int(
                payload, "max_local_reserved_milli_credits"
            ),
            policy_version=_require_int(payload, "policy_version", minimum=1),
            paused_reason=_optional_str(payload, "paused_reason"),
        )


@dataclass(frozen=True)
class NodeHello:
    """Opening message from a connector.

    `device_key_id` names the key that will sign this node's usage reports. The
    center resolves it through its own registry rather than trusting the
    connection, so a node cannot claim a device identity it does not hold.
    """

    node_id: str
    device_id: str
    device_key_id: str
    provider_id: str
    adapter_name: str
    model: str
    policy: NodePolicy
    #: Attempts the node believes it still owns, from its local execution table.
    #: Sent so the center can reconcile an uncertain execution instead of
    #: assuming it never started.
    open_attempts: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical payload."""
        return {
            "node_id": self.node_id,
            "device_id": self.device_id,
            "device_key_id": self.device_key_id,
            "provider_id": self.provider_id,
            "adapter": {"name": self.adapter_name, "model": self.model},
            "policy": self.policy.to_payload(),
            "open_attempts": list(self.open_attempts),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> NodeHello:
        """Parse a `node.hello` payload.

        Raises:
            ProtocolError: If a field is missing or malformed.
        """
        adapter = payload.get("adapter")
        if not isinstance(adapter, dict):
            raise ProtocolError("node.hello requires an adapter object")
        policy = payload.get("policy")
        if not isinstance(policy, dict):
            raise ProtocolError("node.hello requires a policy object")
        open_attempts = payload.get("open_attempts", [])
        if not isinstance(open_attempts, list) or not all(
            isinstance(item, str) for item in open_attempts
        ):
            raise ProtocolError("open_attempts must be a list of attempt IDs")
        return cls(
            node_id=_require_str(payload, "node_id"),
            device_id=_require_str(payload, "device_id"),
            device_key_id=_require_str(payload, "device_key_id"),
            provider_id=_require_str(payload, "provider_id"),
            adapter_name=_require_str(adapter, "name"),
            model=_require_str(adapter, "model"),
            policy=NodePolicy.from_payload(cast("dict[str, Any]", policy)),
            open_attempts=tuple(cast("list[str]", open_attempts)),
        )


@dataclass(frozen=True)
class ResumeDirective:
    """What the center wants the node to do with an unfinished attempt.

    The node must not re-execute an attempt named here unless the directive says
    so; that is how a reconnect stops short of a second upstream call.
    """

    request_id: str
    attempt_id: str
    epoch: int
    action: Literal["resend_spool", "cancel", "abandon"]

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical payload."""
        return {
            "request_id": self.request_id,
            "attempt_id": self.attempt_id,
            "epoch": self.epoch,
            "action": self.action,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ResumeDirective:
        """Parse a resume directive.

        Raises:
            ProtocolError: If the action is not one of the three defined kinds.
        """
        action = payload.get("action")
        if action not in ("resend_spool", "cancel", "abandon"):
            raise ProtocolError(f"unknown resume action {action!r}")
        return cls(
            request_id=_require_str(payload, "request_id"),
            attempt_id=_require_str(payload, "attempt_id"),
            epoch=_require_int(payload, "epoch"),
            action=cast("Literal['resend_spool', 'cancel', 'abandon']", action),
        )


@dataclass(frozen=True)
class HelloAck:
    """Center's reply to `node.hello`."""

    node_id: str
    server_time: dt.datetime
    heartbeat_interval_s: int
    resume: tuple[ResumeDirective, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical payload."""
        return {
            "node_id": self.node_id,
            "server_time": _instant(self.server_time).isoformat(),
            "heartbeat_interval_s": self.heartbeat_interval_s,
            "resume": [directive.to_payload() for directive in self.resume],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> HelloAck:
        """Parse a `hello.ack` payload.

        Raises:
            ProtocolError: If a field is missing or malformed.
        """
        resume = payload.get("resume", [])
        if not isinstance(resume, list):
            raise ProtocolError("resume must be a list")
        return cls(
            node_id=_require_str(payload, "node_id"),
            server_time=_parse_instant(payload.get("server_time"), "server_time"),
            heartbeat_interval_s=_require_int(payload, "heartbeat_interval_s", minimum=1),
            resume=tuple(
                ResumeDirective.from_payload(cast("dict[str, Any]", item)) for item in resume
            ),
        )


@dataclass(frozen=True)
class AttemptOffer:
    """Center's proposal of one attempt to one node.

    `permit_hash` binds the offer's economic terms into a single digest. The node
    echoes it inside its signed usage report, so a report cannot be replayed onto
    a different attempt or a different budget: the center recomputes the digest
    from what it actually offered and refuses a mismatch.
    """

    request_id: str
    attempt_id: str
    epoch: int
    resource_id: str
    model: str
    request_digest: str
    max_milli_credits: int
    deadline_ms: int
    delivery_mode: DeliveryMode
    prompt: str
    authorization_hash: str
    permit_hash: str

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical payload."""
        return {
            "request_id": self.request_id,
            "attempt_id": self.attempt_id,
            "epoch": self.epoch,
            "resource_id": self.resource_id,
            "model": self.model,
            "request_digest": self.request_digest,
            "max_milli_credits": self.max_milli_credits,
            "deadline_ms": self.deadline_ms,
            "delivery_mode": self.delivery_mode,
            "prompt": self.prompt,
            "authorization_hash": self.authorization_hash,
            "permit_hash": self.permit_hash,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AttemptOffer:
        """Parse an `attempt.offer` payload.

        Raises:
            ProtocolError: If a field is missing or the delivery mode is unknown.
        """
        mode = payload.get("delivery_mode")
        if mode not in ("stream_strict", "buffered_complete"):
            raise ProtocolError(f"unknown delivery_mode {mode!r}")
        return cls(
            request_id=_require_str(payload, "request_id"),
            attempt_id=_require_str(payload, "attempt_id"),
            epoch=_require_int(payload, "epoch"),
            resource_id=_require_str(payload, "resource_id"),
            model=_require_str(payload, "model"),
            request_digest=_require_str(payload, "request_digest"),
            max_milli_credits=_require_int(payload, "max_milli_credits"),
            deadline_ms=_require_int(payload, "deadline_ms", minimum=1),
            delivery_mode=cast("DeliveryMode", mode),
            prompt=_require_str(payload, "prompt"),
            authorization_hash=_require_str(payload, "authorization_hash"),
            permit_hash=_require_str(payload, "permit_hash"),
        )


def compute_permit_hash(
    *,
    request_id: str,
    attempt_id: str,
    epoch: int,
    model: str,
    request_digest: str,
    max_milli_credits: int,
    authorization_hash: str,
) -> str:
    """Return the digest binding an offer's economic terms.

    Both sides call this on the same fields; if their notions of "the offer"
    diverge, verification fails loudly instead of silently settling a report
    against the wrong terms.

    Args:
        request_id: The owning request.
        attempt_id: The attempt being offered.
        epoch: Dispatch epoch.
        model: Model the node is asked to run.
        request_digest: Digest of the immutable request payload.
        max_milli_credits: Ceiling the center is willing to pay.
        authorization_hash: Digest of the signed consumer authorization.

    Returns:
        The canonical digest.
    """
    return canonical_digest(
        {
            "kind": "aipotluck.permit.v1",
            "request_id": request_id,
            "attempt_id": attempt_id,
            "epoch": epoch,
            "model": model,
            "request_digest": request_digest,
            "max_milli_credits": max_milli_credits,
            "authorization_hash": authorization_hash,
        }
    )


def compute_acceptance_hash(
    *,
    permit_hash: str,
    request_id: str,
    attempt_id: str,
    epoch: int,
    adapter_name: str,
    accepted_at: dt.datetime,
) -> str:
    """Return the digest of a node's acceptance of an offer.

    The node computes this at ACCEPT time and echoes it in its signed usage
    report. It is the device's commitment to the specific offer it took, so a
    later report cannot claim a different adapter or a different epoch.

    Args:
        permit_hash: The offer digest the node accepted.
        request_id: The owning request.
        attempt_id: The accepted attempt.
        epoch: Dispatch epoch.
        adapter_name: The adapter that will execute.
        accepted_at: When the node accepted.

    Returns:
        The canonical digest.
    """
    return canonical_digest(
        {
            "kind": "aipotluck.acceptance.v1",
            "permit_hash": permit_hash,
            "request_id": request_id,
            "attempt_id": attempt_id,
            "epoch": epoch,
            "adapter_name": adapter_name,
            "accepted_at": _instant(accepted_at).isoformat(),
        }
    )



@dataclass(frozen=True)
class AttemptRef:
    """Minimal reference to one attempt, used by accept/reject/execute/query."""

    request_id: str
    attempt_id: str
    epoch: int

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical payload."""
        return {
            "request_id": self.request_id,
            "attempt_id": self.attempt_id,
            "epoch": self.epoch,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AttemptRef:
        """Parse an attempt reference.

        Raises:
            ProtocolError: If a field is missing.
        """
        return cls(
            request_id=_require_str(payload, "request_id"),
            attempt_id=_require_str(payload, "attempt_id"),
            epoch=_require_int(payload, "epoch"),
        )


@dataclass(frozen=True)
class AttemptDecision:
    """Node's accept/reject of an offer.

    An acceptance carries the node's own digest of what it agreed to. That digest
    travels back inside the signed usage report, so the device ends up signing
    over the exact offer it accepted rather than over whatever the center claims
    it offered.
    """

    ref: AttemptRef
    accepted: bool
    reason: str = ""
    permit_hash: str = ""
    acceptance_hash: str = ""

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical payload."""
        payload: dict[str, Any] = {**self.ref.to_payload(), "reason": self.reason}
        if self.accepted:
            payload["permit_hash"] = self.permit_hash
            payload["acceptance_hash"] = self.acceptance_hash
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, accepted: bool) -> AttemptDecision:
        """Parse an accept or reject payload.

        Args:
            payload: The message payload.
            accepted: Whether the message was `attempt.accept` or
                `attempt.reject`.

        Returns:
            The decision.

        Raises:
            ProtocolError: If the reference is missing, or an acceptance carries
                no commitment hashes.
        """
        ref = AttemptRef.from_payload(payload)
        if not accepted:
            return cls(ref=ref, accepted=False, reason=_optional_str(payload, "reason") or "")
        return cls(
            ref=ref,
            accepted=True,
            reason=_optional_str(payload, "reason") or "",
            permit_hash=_require_str(payload, "permit_hash"),
            acceptance_hash=_require_str(payload, "acceptance_hash"),
        )


@dataclass(frozen=True)
class AttemptState:
    """Node's answer to `attempt.query` about one attempt."""

    ref: AttemptRef
    state: ExecutionState
    certainty: ExecutionCertainty
    upstream_calls: int
    last_seq: int

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical payload."""
        return {
            **self.ref.to_payload(),
            "state": self.state,
            "certainty": self.certainty,
            "upstream_calls": self.upstream_calls,
            "last_seq": self.last_seq,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AttemptState:
        """Parse an `attempt.state` payload.

        Raises:
            ProtocolError: If an enum field carries an unknown value.
        """
        state = payload.get("state")
        if state not in ("SUBMITTING", "SUBMITTED", "FAILED", "UNKNOWN"):
            raise ProtocolError(f"unknown execution state {state!r}")
        certainty = payload.get("certainty")
        if certainty not in ("not_submitted", "submitted", "unknown"):
            raise ProtocolError(f"unknown execution certainty {certainty!r}")
        return cls(
            ref=AttemptRef.from_payload(payload),
            state=cast("ExecutionState", state),
            certainty=cast("ExecutionCertainty", certainty),
            upstream_calls=_require_int(payload, "upstream_calls"),
            last_seq=_require_int(payload, "last_seq"),
        )


@dataclass(frozen=True)
class AttemptEvent:
    """One ordered event from an attempt.

    A delta event carries its text; a usage event carries both the numbers and
    the device's **signed** usage report; a terminal event carries the outcome.
    The numbers travel twice on purpose: the report is what settlement actually
    verifies, and carrying the plain numbers alongside lets the center refuse a
    node that signs one total and announces another.

    Sequence numbers are per attempt and must be strictly increasing from 1, so a
    gap is detectable rather than silently presented as a complete stream.
    """

    ref: AttemptRef
    seq: int
    event_type: EventType
    text: str = ""
    usage: NormalizedUsage | None = None
    report: dict[str, Any] | None = None
    outcome: str | None = None
    detail: str | None = None
    final: bool = False

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical payload.

        Raises:
            ProtocolError: If a usage event carries no usage object or no signed
                report body.
        """
        payload: dict[str, Any] = {
            **self.ref.to_payload(),
            "seq": self.seq,
            "event_type": self.event_type,
            "final": self.final,
        }
        if self.event_type == "output.delta":
            payload["text"] = self.text
        elif self.event_type == "usage.final":
            if self.usage is None:
                raise ProtocolError("a usage.final event requires a usage object")
            if self.report is None:
                raise ProtocolError(
                    "a usage.final event requires a signed usage report; "
                    "an unsigned total is not settlement evidence"
                )
            payload["usage"] = self.usage.to_payload()
            payload["report"] = self.report
        else:
            payload["outcome"] = self.outcome
            payload["detail"] = self.detail
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AttemptEvent:
        """Parse an `attempt.event` payload.

        Raises:
            ProtocolError: If the event type is unknown, the sequence is below 1,
                a delta exceeds the per-event limit, or a required body is
                missing.
        """
        event_type = payload.get("event_type")
        if event_type not in ("output.delta", "usage.final", "attempt.failed", "attempt.completed"):
            raise ProtocolError(f"unknown event_type {event_type!r}")
        seq = _require_int(payload, "seq", minimum=1)
        final = payload.get("final", False)
        if not isinstance(final, bool):
            raise ProtocolError("final must be a boolean")
        event = cls(
            ref=AttemptRef.from_payload(payload),
            seq=seq,
            event_type=cast("EventType", event_type),
            final=final,
        )
        if event_type == "output.delta":
            text = payload.get("text")
            if not isinstance(text, str):
                raise ProtocolError("an output.delta event requires string text")
            if len(text) > MAX_DELTA_CHARS:
                raise ProtocolError(
                    f"output delta of {len(text)} characters exceeds the "
                    f"{MAX_DELTA_CHARS}-character limit; the sender must split it"
                )
            return AttemptEvent(
                ref=event.ref,
                seq=event.seq,
                event_type=event.event_type,
                text=text,
                final=event.final,
            )
        if event_type == "usage.final":
            usage = payload.get("usage")
            if not isinstance(usage, dict):
                raise ProtocolError("a usage.final event requires a usage object")
            report = payload.get("report")
            if not isinstance(report, dict):
                raise ProtocolError(
                    "a usage.final event requires a signed usage report; "
                    "an unsigned total is not settlement evidence"
                )
            return AttemptEvent(
                ref=event.ref,
                seq=event.seq,
                event_type=event.event_type,
                usage=NormalizedUsage.from_payload(cast("dict[str, Any]", usage)),
                report=dict(report),
                final=event.final,
            )
        return AttemptEvent(
            ref=event.ref,
            seq=event.seq,
            event_type=event.event_type,
            outcome=_optional_str(payload, "outcome"),
            detail=_optional_str(payload, "detail"),
            final=event.final,
        )


@dataclass(frozen=True)
class EventAck:
    """Center's acknowledgement of a contiguous prefix of an attempt's events."""

    attempt_id: str
    acked_seq: int

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical payload."""
        return {"attempt_id": self.attempt_id, "acked_seq": self.acked_seq}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> EventAck:
        """Parse an `event.ack` payload.

        Raises:
            ProtocolError: If a field is missing.
        """
        return cls(
            attempt_id=_require_str(payload, "attempt_id"),
            acked_seq=_require_int(payload, "acked_seq"),
        )


@dataclass(frozen=True)
class NodeHeartbeat:
    """Liveness message from a connector, distinct from transport keepalive."""

    in_flight: int
    share_enabled: bool
    at: dt.datetime

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical payload."""
        return {
            "in_flight": self.in_flight,
            "share_enabled": self.share_enabled,
            "at": _instant(self.at).isoformat(),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> NodeHeartbeat:
        """Parse a `node.heartbeat` payload.

        Raises:
            ProtocolError: If a field is missing or malformed.
        """
        share_enabled = payload.get("share_enabled")
        if not isinstance(share_enabled, bool):
            raise ProtocolError("share_enabled must be a boolean")
        return cls(
            in_flight=_require_int(payload, "in_flight"),
            share_enabled=share_enabled,
            at=_parse_instant(payload.get("at"), "at"),
        )


@dataclass(frozen=True)
class ProtocolErrorBody:
    """Body of a `protocol.error` message."""

    code: str
    detail: str

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical payload."""
        return {"code": self.code, "detail": self.detail}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ProtocolErrorBody:
        """Parse a `protocol.error` payload.

        Raises:
            ProtocolError: If a field is missing.
        """
        return cls(
            code=_require_str(payload, "code"),
            detail=_require_str(payload, "detail"),
        )


def ack_envelope(ref: AttemptRef, acked_seq: int) -> Envelope:
    """Build an `event.ack` envelope for `ref`."""
    return Envelope(
        type="event.ack",
        payload=EventAck(attempt_id=ref.attempt_id, acked_seq=acked_seq).to_payload(),
    )


def error_envelope(code: str, detail: str, *, reply_to: str | None = None) -> Envelope:
    """Build a `protocol.error` envelope.

    Args:
        code: Short machine-readable code, e.g. ``unknown_attempt``.
        detail: Operator-facing explanation.
        reply_to: Message ID being rejected, when known.

    Returns:
        The envelope to send back.
    """
    return Envelope(
        type="protocol.error",
        payload=ProtocolErrorBody(code=code, detail=detail).to_payload(),
        reply_to=reply_to,
    )


def require_same_attempt(ref: AttemptRef, payload: dict[str, Any], field: str) -> None:
    """Check that a payload's attempt reference matches `ref`.

    A message that names a different attempt than the connection is working on is
    a protocol violation, not a message about another attempt.

    Raises:
        ProtocolError: If the reference does not match.
    """
    other = AttemptRef.from_payload(payload)
    if (other.request_id, other.attempt_id) != (ref.request_id, ref.attempt_id):
        raise ProtocolError(
            f"{field} refers to attempt {other.attempt_id} of request "
            f"{other.request_id}, expected {ref.attempt_id} of {ref.request_id}"
        )
