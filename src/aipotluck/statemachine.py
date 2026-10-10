"""Request, attempt, output, and settlement state machines.

These evolve independently on purpose. Output success does not imply metering
is complete, and a failed attempt can still have complete usage. Collapsing them
into one status is how platforms end up either re-running a model call to chase
a usage field or dropping a real consumption record.

The recovery decision table is separate from the state machines because the
question it answers — may we retry on another resource? — depends on whether
output was committed, not merely on whether the attempt failed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from .errors import StateTransitionError

RequestStatus = Literal[
    "CREATED", "RESERVED", "ROUTING", "RUNNING", "RECOVERING",
    "SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED",
]
AttemptStatus = Literal[
    "PREPARED", "OFFERED", "ACCEPTED", "AUTHORIZED", "SUBMITTING", "RUNNING",
    "COMPLETED", "FAILED", "CANCELLED", "OUTCOME_UNKNOWN", "REVOKED_BEFORE_SUBMIT",
]
ExecutionCertainty = Literal["not_submitted", "submitted", "unknown"]
OutputCommitStatus = Literal["UNCOMMITTED", "COMMITTED", "TERMINAL"]
UsageStatus = Literal["EMPTY", "PROVISIONAL", "COMPLETE", "PARTIAL", "UNKNOWN"]
SettlementStatus = Literal[
    "HELD", "PENDING_USAGE", "SETTLED", "CLOSED_WITH_KNOWN_COMPONENTS", "CLOSED_UNBILLED",
]
RecoveryAction = Literal[
    "replay_result",
    "resume_execution",
    "retry_on_another_resource",
    "fail_explicitly",
    "await_recovery",
]

TERMINAL_REQUEST_STATES: frozenset[str] = frozenset(
    {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}
)
TERMINAL_ATTEMPT_STATES: frozenset[str] = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED", "OUTCOME_UNKNOWN", "REVOKED_BEFORE_SUBMIT"}
)

_REQUEST_TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"RESERVED", "CANCELLED", "EXPIRED"}),
    "RESERVED": frozenset({"ROUTING", "CANCELLED", "EXPIRED"}),
    "ROUTING": frozenset({"RUNNING", "FAILED", "CANCELLED", "EXPIRED"}),
    "RUNNING": frozenset({"RECOVERING", "SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}),
    "RECOVERING": frozenset({"RUNNING", "FAILED", "CANCELLED", "EXPIRED"}),
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
    "EXPIRED": frozenset(),
}

_ATTEMPT_TRANSITIONS: dict[str, frozenset[str]] = {
    "PREPARED": frozenset({"OFFERED", "REVOKED_BEFORE_SUBMIT"}),
    "OFFERED": frozenset({"ACCEPTED", "REVOKED_BEFORE_SUBMIT", "FAILED"}),
    "ACCEPTED": frozenset({"AUTHORIZED", "REVOKED_BEFORE_SUBMIT", "FAILED"}),
    "AUTHORIZED": frozenset({"SUBMITTING", "REVOKED_BEFORE_SUBMIT", "FAILED"}),
    "SUBMITTING": frozenset({"RUNNING", "COMPLETED", "FAILED", "OUTCOME_UNKNOWN"}),
    "RUNNING": frozenset({"COMPLETED", "FAILED", "CANCELLED", "OUTCOME_UNKNOWN"}),
    "COMPLETED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
    "OUTCOME_UNKNOWN": frozenset(),
    "REVOKED_BEFORE_SUBMIT": frozenset(),
}


@dataclass
class Request:
    """One logical user intent.

    `dispatch_epoch` fences stale output branches: when the coordinator fails
    over from A to B it increments the epoch, and late output from A is rejected
    for publication while its usage is still accepted.
    """

    request_id: str
    tenant_id: str
    idempotency_key: str
    payload_digest: str
    status: RequestStatus = "CREATED"
    selected_attempt_id: str | None = None
    dispatch_epoch: int = 0
    delivery_mode: Literal["stream_strict", "buffered_complete"] = "stream_strict"
    duplicate_execution_policy: str = "bounded_model_only"
    max_attempts: int = 2
    cancel_origin: str | None = None

    def transition(self, target: RequestStatus) -> None:
        """Move to a new request status.

        Raises:
            StateTransitionError: If the transition is not allowed, including
                any move out of a terminal state.
        """
        allowed = _REQUEST_TRANSITIONS.get(self.status, frozenset())
        if target not in allowed:
            raise StateTransitionError(
                f"request {self.request_id}: {self.status} -> {target} is not allowed "
                f"(permitted: {sorted(allowed) or 'terminal'})"
            )
        self.status = target

    def is_terminal(self) -> bool:
        """Report whether the request has reached a terminal state."""
        return self.status in TERMINAL_REQUEST_STATES


@dataclass
class Attempt:
    """One execution try against one resource.

    A retry always creates a new attempt ID, because two upstream calls consume
    two sets of tokens even when only one result is delivered.
    """

    attempt_id: str
    request_id: str
    resource_id: str
    provider_id: str
    dispatch_epoch: int
    status: AttemptStatus = "PREPARED"
    execution_certainty: ExecutionCertainty = "not_submitted"
    output_eligible: bool = True
    superseded_by: str | None = None
    usage_status: UsageStatus = "EMPTY"
    settlement_status: SettlementStatus = "HELD"

    def transition(self, target: AttemptStatus) -> None:
        """Move to a new attempt status.

        Raises:
            StateTransitionError: If the transition is not allowed.
        """
        allowed = _ATTEMPT_TRANSITIONS.get(self.status, frozenset())
        if target not in allowed:
            raise StateTransitionError(
                f"attempt {self.attempt_id}: {self.status} -> {target} is not allowed "
                f"(permitted: {sorted(allowed) or 'terminal'})"
            )
        self.status = target

    def is_terminal(self) -> bool:
        """Report whether the attempt has reached a terminal state."""
        return self.status in TERMINAL_ATTEMPT_STATES


@dataclass
class OutputCommit:
    """Output publication state for a request.

    Commit happens when the first semantic output segment is persisted and bound
    to a winner, before it is written to the socket. That ordering means a
    dropped connection after the write cannot be mistaken for "the user never
    saw it". Comment-only keepalives are not semantic output; anything that
    cannot be classified is treated conservatively as committed.
    """

    status: OutputCommitStatus = "UNCOMMITTED"
    attempt_id: str | None = None
    output_generation_id: str | None = None
    committed_at_seq: int | None = None

    def commit(self, attempt_id: str, generation_id: str, seq: int) -> None:
        """Bind the first semantic output segment to an attempt.

        Raises:
            StateTransitionError: If output was already committed, which would
                give a request two live generation branches.
        """
        if self.status != "UNCOMMITTED":
            raise StateTransitionError(
                f"output already {self.status}; a request may have only one committed branch"
            )
        self.status = "COMMITTED"
        self.attempt_id = attempt_id
        self.output_generation_id = generation_id
        self.committed_at_seq = seq

    def finalize(self) -> None:
        """Mark the committed branch terminal after delivery completes."""
        if self.status == "COMMITTED":
            self.status = "TERMINAL"


@dataclass
class RequestAggregate:
    """Request plus its attempts, output commit, and usage/settlement state."""

    request: Request
    attempts: dict[str, Attempt] = field(default_factory=dict)
    output: OutputCommit = field(default_factory=OutputCommit)
    usage_status: UsageStatus = "EMPTY"
    settlement_status: SettlementStatus = "HELD"
    known_attempt_tokens: dict[str, int] = field(default_factory=dict)
    unknown_attempts: int = 0

    def current_attempt(self) -> Attempt | None:
        """Return the attempt that currently owns output eligibility."""
        for attempt in self.attempts.values():
            if attempt.output_eligible:
                return attempt
        return None

    def failover(self, new_attempt: Attempt, reason: str) -> Attempt:
        """Supersede the current attempt and register a new one.

        The old attempt keeps its execution and usage state; it only loses the
        right to publish output. Late output from it is refused, but a late usage
        report is still accepted so real consumption is never silently dropped.

        The replacement may arrive already registered, because the natural way to
        build one is `Coordinator.open_attempt`, which registers as it goes. That
        case must not supersede the branch being installed: doing so would leave
        the request with no output-eligible attempt at all, and nothing could
        ever publish a result.

        Args:
            new_attempt: The replacement attempt, registered or not.
            reason: Why the failover happened, recorded for audit.

        Returns:
            The new attempt.

        Raises:
            StateTransitionError: If output was already committed, or the
                attempt budget is exhausted.
        """
        if self.output.status != "UNCOMMITTED":
            raise StateTransitionError(
                f"cannot fail over after output {self.output.status}; "
                "committed output must not be spliced with another branch"
            )
        already_registered = new_attempt.attempt_id in self.attempts
        # An already-registered replacement already consumed a slot, so only
        # count a genuinely new attempt against the budget.
        if not already_registered and len(self.attempts) >= self.request.max_attempts:
            raise StateTransitionError(
                f"attempt budget exhausted ({self.request.max_attempts}); "
                "failures must not become unbounded retries"
            )
        current = self.current_attempt()
        if current is not None and current.attempt_id != new_attempt.attempt_id:
            current.output_eligible = False
            current.superseded_by = new_attempt.attempt_id
        # The epoch advances on every failover, whether or not the replacement
        # was pre-registered: it is the audit record that a switch happened, and
        # it fences any output the old branch may still try to publish.
        self.request.dispatch_epoch += 1
        if not already_registered:
            self.attempts[new_attempt.attempt_id] = new_attempt
        new_attempt.dispatch_epoch = self.request.dispatch_epoch
        new_attempt.output_eligible = True
        new_attempt.superseded_by = None
        return new_attempt

    def total_known_tokens(self) -> int:
        """Return the sum of all attempts' known usage.

        This is resource consumption, not the consumer's bill. A failed attempt
        still consumed tokens and must appear here.
        """
        return sum(self.known_attempt_tokens.values())

    def record_usage(self, attempt_id: str, tokens: int) -> None:
        """Record an attempt's known usage and update the request's usage status."""
        self.known_attempt_tokens[attempt_id] = tokens
        self.attempts[attempt_id].usage_status = "COMPLETE"
        if self.unknown_attempts == 0:
            self.usage_status = "COMPLETE"
        else:
            self.usage_status = "PARTIAL"

    def mark_unknown_attempt(self) -> None:
        """Record that one attempt's usage cannot be determined."""
        self.unknown_attempts += 1
        self.usage_status = "PARTIAL" if self.known_attempt_tokens else "UNKNOWN"

    def billable_tokens(self) -> int:
        """Return the tokens the consumer is billed for.

        Only the winner's successful service is billable by default. Failed
        attempts stay in the resource record but do not become the consumer's
        bill, which is what keeps a platform-initiated retry from being charged
        twice.
        """
        winner = self.output.attempt_id
        if winner is None:
            return 0
        return self.known_attempt_tokens.get(winner, 0)

    def status_view(self) -> dict[str, object]:
        """Return the status and billing view described in the protocol."""
        return {
            "request_id": self.request.request_id,
            "status": self.request.status,
            "selected_attempt_id": self.output.attempt_id,
            "result_available": self.output.status in {"COMMITTED", "TERMINAL"},
            "delivery_mode": self.request.delivery_mode,
            "usage": {
                "selected_service_tokens": self.billable_tokens(),
                "all_attempts_known_tokens": self.total_known_tokens(),
                "unknown_attempts": self.unknown_attempts,
            },
            "billing": {
                "status": self.settlement_status,
            },
        }


@dataclass(frozen=True)
class RecoveryContext:
    """Everything the decision table needs to choose a recovery action."""

    attempt_status: AttemptStatus
    execution_certainty: ExecutionCertainty
    output_committed: bool
    buffered_mode: bool
    original_execution_resumable: bool
    input_replayable: bool
    deadline_remaining_ms: int
    has_external_side_effects: bool
    duplicate_execution_policy: str


def decide_recovery(context: RecoveryContext) -> RecoveryAction:
    """Choose the recovery action for a failed attempt.

    This is the design's precision table. The distinctions matter: replaying a
    stored result adds no tokens, resuming the same execution adds only that
    execution's own usage, and retrying elsewhere adds a new attempt whose input
    is processed again.

    `COMPLETED` is tested first because it is the one status that carries a
    finished result. Replaying that result is free and never risks a second
    execution, so it must win even when output was already committed — otherwise
    a completed attempt would be reported as an explicit failure and the client
    would be told to give up on a result that is sitting there. The
    `output_committed` guard exists to stop a *second execution* from being
    spliced behind published output, which replaying a stored result does not do.

    Args:
        context: The observable facts about the failure.

    Returns:
        The chosen action.
    """
    if context.has_external_side_effects:
        return "fail_explicitly"

    if context.attempt_status == "COMPLETED":
        return "replay_result"

    if context.output_committed:
        # Output is already public, so another branch can never be spliced
        # behind it. Prefer recovering the original execution; when it cannot
        # be recovered the client must see a stream error, not an open-ended
        # wait, so the request terminates with an explicit failure.
        if context.original_execution_resumable:
            return "resume_execution"
        return "fail_explicitly"

    if (
        context.attempt_status in {"PREPARED", "OFFERED", "ACCEPTED", "AUTHORIZED"}
        and context.execution_certainty == "not_submitted"
    ):
        return "retry_on_another_resource"

    if context.attempt_status in {"SUBMITTING", "RUNNING", "OUTCOME_UNKNOWN"}:
        if context.original_execution_resumable:
            return "resume_execution"
        if context.duplicate_execution_policy == "deny":
            # Strict mode: an unverifiable execution is never joined by a second one.
            return "await_recovery"
        if context.deadline_remaining_ms <= 0:
            return "fail_explicitly"
        if not context.input_replayable:
            return "fail_explicitly"
        if context.buffered_mode:
            # Nothing was published, so an internal retry stays invisible.
            return "retry_on_another_resource"
        return "await_recovery"

    if context.attempt_status in {"FAILED", "CANCELLED"}:
        if context.execution_certainty == "not_submitted" and context.input_replayable:
            return "retry_on_another_resource"
        if (
            not context.output_committed
            and context.buffered_mode
            and context.input_replayable
            and context.deadline_remaining_ms > 0
        ):
            # A clean failure with nothing published and no outside side effects
            # is safe to retry once elsewhere: the consumer only ever sees the
            # eventual result, and the failed branch's usage is still recorded.
            # This is what makes a real node failure a replacement offer rather
            # than a dead request.
            return "retry_on_another_resource"
        return "fail_explicitly"

    return "fail_explicitly"


def replay_costs_tokens(action: RecoveryAction) -> bool:
    """Report whether an action adds billable model usage.

    Args:
        action: The recovery action.

    Returns:
        True only for actions that create new upstream consumption.
    """
    return action == "retry_on_another_resource"


@dataclass(frozen=True)
class CancelDecision:
    """How a termination is billed, keyed by who caused it."""

    origin: str
    charge_consumer: bool
    provider_earns: bool
    note: str


def decide_cancel(origin: str, usage_complete: bool, within_authorization: bool) -> CancelDecision:
    """Decide billing for a cancellation.

    A consumer cancel settles the usage that acceptable evidence covers, bounded
    by the pre-authorization. A contributor stop is treated as a node fault, so
    the contributor bears it.

    Args:
        origin: One of `consumer_cancel`, `consumer_disconnect_cancel`,
            `provider_stop`, `deadline`, or `budget_stop`.
        usage_complete: Whether the final usage report is available.
        within_authorization: Whether the covered usage stays under the cap.

    Returns:
        The billing decision.

    Raises:
        ValueError: If the origin is unknown.
    """
    if origin in {"consumer_cancel", "consumer_disconnect_cancel"}:
        if not usage_complete:
            return CancelDecision(
                origin=origin,
                charge_consumer=False,
                provider_earns=False,
                note="usage unknown; settle only determinable components and record the rest",
            )
        if not within_authorization:
            return CancelDecision(
                origin=origin,
                charge_consumer=True,
                provider_earns=True,
                note="capped at the pre-authorized maximum; excess stays a resource cost",
            )
        return CancelDecision(
            origin=origin,
            charge_consumer=True,
            provider_earns=True,
            note="settle the usage covered by acceptable evidence",
        )
    if origin == "provider_stop":
        return CancelDecision(
            origin=origin,
            charge_consumer=False,
            provider_earns=False,
            note="contributor fault; consumption stays with the contributor",
        )
    if origin in {"deadline", "budget_stop"}:
        if usage_complete and within_authorization:
            return CancelDecision(
                origin=origin,
                charge_consumer=True,
                provider_earns=True,
                note="service completed within the accepted budget",
            )
        return CancelDecision(
            origin=origin,
            charge_consumer=False,
            provider_earns=False,
            note="no complete deliverable; record consumption without charging",
        )
    raise ValueError(f"unknown cancel origin: {origin}")


class CancelOrigin(StrEnum):
    """Enumerated cancellation origins, kept separate for the audit trail."""

    CONSUMER_CANCEL = "consumer_cancel"
    CONSUMER_DISCONNECT = "consumer_disconnect_cancel"
    PROVIDER_STOP = "provider_stop"
    DEADLINE = "deadline"
    BUDGET_STOP = "budget_stop"


def reconcile_deadline(
    reconcile_at: dt.datetime,
    requested_at: dt.datetime,
    window_hours: int = 24,
) -> bool:
    """Report whether the usage reconciliation window has closed.

    Late reports still update the resource record after this point, but the
    consumer's bill is not silently increased.
    """
    return reconcile_at >= requested_at + dt.timedelta(hours=window_hours)