"""Coordinator: dispatch, failover, and the output-commit race.

The coordinator is the only layer allowed to decide to retry. Connectors and
consumer SDKs expose or disable their own retries, because "tool retry x
coordinator retry x CLI retry" is how one request becomes three upstream calls.

The load-bearing detail is that committing the first output segment and failing
over must compete on the same lock. If commit wins, no retry happens; if
failover wins, the stale branch's output is refused. Running them without shared
state is how a user ends up with half an answer from A glued to half from B.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .errors import ConflictError, StateTransitionError
from .statemachine import (
    Attempt,
    RecoveryContext,
    Request,
    RequestAggregate,
    RequestStatus,
    decide_recovery,
)

CancelOrigin = Literal[
    "consumer_cancel", "consumer_disconnect_cancel", "provider_stop", "deadline", "budget_stop"
]


class EventStore(Protocol):
    """Append-only event sink shared by the coordinator and connectors."""

    def append(
        self, attempt_id: str, seq: int, event_type: str, payload: dict[str, object]
    ) -> str:
        """Append an event, returning its event ID.

        Returns an existing ID for an identical replay, and raises `ConflictError`
        when the same sequence number carries different content.
        """
        ...


@dataclass
class InMemoryEventStore:
    """Event store that enforces per-attempt sequence integrity.

    Replaying the same `(attempt_id, seq)` with identical content is a no-op, so
    at-least-once delivery does not double-count. Different content at the same
    sequence is a protocol violation and is refused rather than overwritten.
    """

    _events: dict[str, dict[int, str]] = field(default_factory=dict)
    _digests: dict[tuple[str, int], str] = field(default_factory=dict)
    _counter: int = 0

    def append(
        self, attempt_id: str, seq: int, event_type: str, payload: dict[str, object]
    ) -> str:
        """Append or deduplicate an event.

        Args:
            attempt_id: The owning attempt.
            seq: Monotonic per-attempt sequence number.
            event_type: Event kind, e.g. ``output.delta``.
            payload: Event body.

        Returns:
            The event ID ``attempt_id:seq``.

        Raises:
            ConflictError: If the sequence already holds different content.
        """
        from .canonical import canonical_digest

        digest = canonical_digest({"type": event_type, "payload": payload})
        existing = self._digests.get((attempt_id, seq))
        if existing is not None:
            if existing != digest:
                raise ConflictError(
                    f"attempt {attempt_id} sequence {seq} already holds different content; "
                    "a sequence number may not be reused with new data"
                )
            return f"{attempt_id}:{seq}"
        self._digests[(attempt_id, seq)] = digest
        self._events.setdefault(attempt_id, {})[seq] = event_type
        self._counter += 1
        return f"{attempt_id}:{seq}"

    def count(self, attempt_id: str) -> int:
        """Return the number of stored events for an attempt."""
        return len(self._events.get(attempt_id, {}))

    def contiguous_prefix(self, attempt_id: str) -> int:
        """Return the highest sequence with no gap below it.

        A gap means the stream is incomplete; callers must not present a partial
        stream as a complete answer.
        """
        seqs = self._events.get(attempt_id, {})
        prefix = 0
        while prefix + 1 in seqs:
            prefix += 1
        return prefix

    def has_gap(self, attempt_id: str) -> bool:
        """Report whether the attempt's event stream has a sequence gap."""
        seqs = self._events.get(attempt_id, {})
        return bool(seqs) and max(seqs) != len(seqs)


class LocalPolicy:
    """Contribution-side limits. The platform cannot raise any of them.

    A valid platform signature does not permit work that exceeds a local limit;
    the contributor's own settings are the final authority.
    """

    def __init__(
        self,
        share_enabled: bool = True,
        max_concurrency: int = 1,
        max_local_reserved_milli_credits: int = 20_000,
        policy_version: int = 1,
    ) -> None:
        self.share_enabled = share_enabled
        self.max_concurrency = max_concurrency
        self.max_local_reserved_milli_credits = max_local_reserved_milli_credits
        self.policy_version = policy_version
        self._in_flight = 0
        self._local_reserved = 0

    def admit(self, requested_milli_credits: int) -> tuple[bool, str]:
        """Check whether a local offer may be admitted.

        Args:
            requested_milli_credits: The attempt's maximum exposure.

        Returns:
            Tuple of (admitted, reason). The reason is operator-facing.
        """
        if not self.share_enabled:
            return False, "sharing is paused on this device"
        if self._in_flight >= self.max_concurrency:
            return (
                False,
                f"local concurrency limit reached ({self._in_flight}/{self.max_concurrency})",
            )
        if self._local_reserved + requested_milli_credits > self.max_local_reserved_milli_credits:
            return (
                False,
                f"local budget would be exceeded: {self._local_reserved} + "
                f"{requested_milli_credits} > {self.max_local_reserved_milli_credits}",
            )
        return True, "admitted"

    def begin(self, requested_milli_credits: int) -> None:
        """Record an admitted attempt against local limits.

        Raises:
            StateTransitionError: If the attempt was not admitted.
        """
        admitted, reason = self.admit(requested_milli_credits)
        if not admitted:
            raise StateTransitionError(f"cannot begin: {reason}")
        self._in_flight += 1
        self._local_reserved += requested_milli_credits

    def end(self, actual_milli_credits: int) -> None:
        """Release local limits after an attempt ends.

        Args:
            actual_milli_credits: The exposure to settle against; may exceed the
                reservation for a best-effort adapter.

        Raises:
            StateTransitionError: If no attempt is in flight.
        """
        if self._in_flight == 0:
            raise StateTransitionError("no attempt is in flight")
        self._in_flight -= 1
        self._local_reserved = max(0, self._local_reserved - actual_milli_credits)


@dataclass
class DispatchDecision:
    """Outcome of evaluating a failed attempt."""

    action: str
    reason: str
    new_attempt: Attempt | None = None


class Coordinator:
    """Coordinates one logical request across attempts and resources."""

    def __init__(self, events: InMemoryEventStore | None = None) -> None:
        self.events = events or InMemoryEventStore()
        self._lock = threading.RLock()
        self._aggregates: dict[str, RequestAggregate] = {}

    def create_request(
        self,
        request_id: str,
        tenant_id: str,
        idempotency_key: str,
        payload_digest: str,
        delivery_mode: Literal["stream_strict", "buffered_complete"] = "stream_strict",
        max_attempts: int = 2,
    ) -> RequestAggregate:
        """Register a new logical request.

        Args:
            request_id: The request ID.
            tenant_id: Owning tenant.
            idempotency_key: Client-generated key scoped to the tenant.
            payload_digest: Digest of the immutable payload.
            delivery_mode: `stream_strict` or `buffered_complete`.
            max_attempts: Hard cap on attempts.

        Returns:
            The aggregate tracking the request.

        Raises:
            ConflictError: If the idempotency key is reused with a different payload.
        """
        with self._lock:
            for aggregate in self._aggregates.values():
                existing = aggregate.request
                if (
                    existing.tenant_id == tenant_id
                    and existing.idempotency_key == idempotency_key
                ):
                    if existing.payload_digest != payload_digest:
                        raise ConflictError(
                            f"idempotency key {idempotency_key} was used for a different "
                            f"payload ({existing.payload_digest} vs {payload_digest})"
                        )
                    return aggregate
            request = Request(
                request_id=request_id,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                payload_digest=payload_digest,
                delivery_mode=delivery_mode,
                max_attempts=max_attempts,
            )
            aggregate = RequestAggregate(request=request)
            self._aggregates[request_id] = aggregate
            return aggregate

    def open_attempt(
        self,
        request_id: str,
        attempt_id: str,
        resource_id: str,
        provider_id: str,
    ) -> Attempt:
        """Create and register a new attempt, becoming the output-eligible branch.

        This is the second entry point that can create a live generation, so it
        enforces the same two invariants as `RequestAggregate.failover`:

        * a request has at most one output-eligible branch, so no branch may be
          opened after output is committed;
        * a request never exceeds its `max_attempts` budget, counting the
          attempts already opened, so no entry point can turn failures into
          unbounded retries.

        The first attempt on a fresh request and a retry opened through
        `handle_attempt_failure` both satisfy these checks and keep working.

        Args:
            request_id: The owning request.
            attempt_id: The new attempt ID.
            resource_id: The resource being used.
            provider_id: The contributor.

        Returns:
            The registered attempt.

        Raises:
            StateTransitionError: If output is already committed, or the attempt
                budget is exhausted.
        """
        with self._lock:
            aggregate = self._require(request_id)
            if aggregate.output.status != "UNCOMMITTED":
                raise StateTransitionError(
                    f"cannot open attempt after output {aggregate.output.status}; "
                    "committed output must not be spliced with another branch"
                )
            if len(aggregate.attempts) >= aggregate.request.max_attempts:
                raise StateTransitionError(
                    f"attempt budget exhausted ({aggregate.request.max_attempts}); "
                    "failures must not become unbounded retries"
                )
            attempt = Attempt(
                attempt_id=attempt_id,
                request_id=request_id,
                resource_id=resource_id,
                provider_id=provider_id,
                dispatch_epoch=aggregate.request.dispatch_epoch,
            )
            aggregate.attempts[attempt_id] = attempt
            current = aggregate.current_attempt()
            if current is not None and current.attempt_id != attempt_id:
                current.output_eligible = False
                current.superseded_by = attempt_id
            return attempt

    def commit_output(
        self,
        request_id: str,
        attempt_id: str,
        seq: int,
        generation_id: str,
        event_payload: dict[str, object] | None = None,
    ) -> bool:
        """Publish the first semantic output segment for a request.

        The commit and the epoch check happen under one lock, so a concurrent
        failover either loses the race (and the old output is refused) or wins it
        (and no failover is allowed).

        Args:
            request_id: The owning request.
            attempt_id: The attempt producing output.
            seq: The attempt-local sequence number.
            generation_id: Identifier for this output generation.
            event_payload: Optional semantic event to persist.

        Returns:
            True when this call committed the output; False when it lost the race.

        Raises:
            StateTransitionError: If the attempt is not output-eligible.
        """
        with self._lock:
            aggregate = self._require(request_id)
            attempt = aggregate.attempts.get(attempt_id)
            if attempt is None:
                raise StateTransitionError(f"unknown attempt {attempt_id}")
            if not attempt.output_eligible:
                return False
            if attempt.dispatch_epoch != aggregate.request.dispatch_epoch:
                return False
            if aggregate.output.status != "UNCOMMITTED":
                return False
            aggregate.output.commit(attempt_id, generation_id, seq)
            if event_payload is not None:
                self.events.append(attempt_id, seq, "output.delta", event_payload)
            aggregate.request.selected_attempt_id = attempt_id
            return True

    def handle_attempt_failure(
        self,
        request_id: str,
        attempt_id: str,
        context: RecoveryContext,
        new_attempt_factory: Callable[[int], Attempt] | None = None,
    ) -> DispatchDecision:
        """Decide and apply recovery for a failed attempt.

        The old attempt loses output eligibility but keeps its execution and
        usage state, so a late usage report from a superseded branch is still
        recorded.

        Recovery is only well-defined for the branch that currently owns output
        eligibility: a superseded attempt's failure says nothing about the live
        branch, so recovering from it would splice a new generation behind a
        branch that never actually failed. This method therefore refuses any
        attempt that is not the current output-eligible branch, and any
        `request_id`/`attempt_id` pair that does not match the stored attempt.

        Args:
            request_id: The owning request.
            attempt_id: The failing attempt, which must be the current branch.
            context: Facts driving the decision table.
            new_attempt_factory: Builds the replacement attempt given an epoch;
                required when the decision is to retry.

        Returns:
            The decision taken.

        Raises:
            StateTransitionError: If the attempt is unknown, belongs to another
                request, is not the current output-eligible branch, or if a
                retry is decided without a factory.
        """
        with self._lock:
            aggregate = self._require(request_id)
            attempt = aggregate.attempts.get(attempt_id)
            if attempt is None:
                raise StateTransitionError(f"unknown attempt {attempt_id}")
            if attempt.request_id != request_id:
                raise StateTransitionError(
                    f"attempt {attempt_id} belongs to request {attempt.request_id}, "
                    f"not {request_id}"
                )
            current = aggregate.current_attempt()
            if current is None or current.attempt_id != attempt_id:
                live = current.attempt_id if current is not None else "none"
                raise StateTransitionError(
                    f"attempt {attempt_id} is not the current output-eligible branch "
                    f"(current: {live}); refusing to recover a superseded attempt"
                )
            action = decide_recovery(context)
            if action != "retry_on_another_resource":
                return DispatchDecision(action=action, reason=_reason_for(action))
            if new_attempt_factory is None:
                raise StateTransitionError(
                    "a retry was decided but no replacement attempt factory was supplied"
                )
            epoch = aggregate.request.dispatch_epoch + 1
            try:
                candidate = new_attempt_factory(epoch)
                aggregate.failover(candidate, reason=_reason_for(action))
            except StateTransitionError as exc:
                # The factory may build the attempt through `open_attempt`, which
                # refuses for the same reasons `failover` does (output committed,
                # budget exhausted). Both refusals mean no safe retry exists, so
                # they surface as an explicit failure rather than escaping.
                return DispatchDecision(action="fail_explicitly", reason=str(exc))
            return DispatchDecision(
                action=action,
                reason=_reason_for(action),
                new_attempt=candidate,
            )

    def record_usage(self, request_id: str, attempt_id: str, tokens: int) -> None:
        """Record an attempt's usage, including for a superseded attempt.

        Args:
            request_id: The owning request.
            attempt_id: The reporting attempt, which may no longer be eligible.
            tokens: Normalized total tokens.

        Raises:
            StateTransitionError: If the attempt is unknown.
        """
        with self._lock:
            aggregate = self._require(request_id)
            if attempt_id not in aggregate.attempts:
                raise StateTransitionError(f"unknown attempt {attempt_id}")
            aggregate.record_usage(attempt_id, tokens)

    def mark_usage_unknown(self, request_id: str, attempt_id: str) -> None:
        """Record that an attempt's final usage cannot be determined.

        The attempt stays in the resource record with unknown usage. It is never
        dropped, and it never becomes zero, so the status view reports a pending
        component rather than pretending the request only cost the winner's
        tokens.

        Args:
            request_id: The owning request.
            attempt_id: The attempt whose usage is unknown.

        Raises:
            StateTransitionError: If the attempt is unknown.
        """
        with self._lock:
            aggregate = self._require(request_id)
            if attempt_id not in aggregate.attempts:
                raise StateTransitionError(f"unknown attempt {attempt_id}")
            aggregate.attempts[attempt_id].usage_status = "UNKNOWN"
            aggregate.mark_unknown_attempt()

    def cancel(
        self,
        request_id: str,
        origin: CancelOrigin,
        usage_complete: bool,
        within_authorization: bool,
    ) -> object:
        """Cancel a request and record why billing follows this path.

        Args:
            request_id: The request to cancel.
            origin: Who caused the cancellation.
            usage_complete: Whether final usage is known.
            within_authorization: Whether covered usage stays under the cap.

        Returns:
            The cancel decision from the billing rules.

        Raises:
            StateTransitionError: If the request is already terminal.
        """
        from .statemachine import decide_cancel

        with self._lock:
            aggregate = self._require(request_id)
            if aggregate.request.is_terminal():
                raise StateTransitionError(
                    f"request {request_id} is already {aggregate.request.status}"
                )
            aggregate.request.cancel_origin = origin
            target: RequestStatus = (
                "CANCELLED" if origin != "deadline" else "EXPIRED"
            )
            aggregate.request.transition(target)
            return decide_cancel(origin, usage_complete, within_authorization)

    def _require(self, request_id: str) -> RequestAggregate:
        aggregate = self._aggregates.get(request_id)
        if aggregate is None:
            raise StateTransitionError(f"unknown request {request_id}")
        return aggregate


def _reason_for(action: str) -> str:
    return {
        "replay_result": "result already persisted; replay adds no model usage",
        "resume_execution": "original execution can resume; retrying would double-consume",
        "retry_on_another_resource": "no output committed and input is replayable",
        "continue_from_partial": "explicit new task on partial output",
        "fail_explicitly": "no safe automatic action; surface the failure",
        "await_recovery": "strict duplicate policy: wait for the original execution",
    }.get(action, action)