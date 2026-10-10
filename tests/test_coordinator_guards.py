"""Guard-bypass regression tests for the coordinator.

The invariant under test is stated once and enforced everywhere:

    a request has at most one output-eligible branch, and its `max_attempts`
    budget holds no matter which entry point is used to create an attempt.

`RequestAggregate.failover` already encodes both rules. `Coordinator.open_attempt`
and `Coordinator.handle_attempt_failure` are the other two doors into the same
state, and these tests pin that they cannot be used to get around `failover`.
"""

from __future__ import annotations

import pytest

from aipotluck.coordinator import Coordinator
from aipotluck.errors import StateTransitionError
from aipotluck.statemachine import Attempt, RecoveryContext


def _context(**overrides: object) -> RecoveryContext:
    """Build a recovery context that defaults to the retry-on-another-resource case."""
    defaults: dict[str, object] = {
        "attempt_status": "ACCEPTED",
        "execution_certainty": "not_submitted",
        "output_committed": False,
        "buffered_mode": False,
        "original_execution_resumable": False,
        "input_replayable": True,
        "deadline_remaining_ms": 30_000,
        "has_external_side_effects": False,
        "duplicate_execution_policy": "bounded_model_only",
    }
    defaults.update(overrides)
    return RecoveryContext(**defaults)  # type: ignore[arg-type]


# -- open_attempt enforces the failover constraints ------------------------


def test_open_attempt_after_commit_is_refused() -> None:
    """A new branch may not be opened once output is committed."""
    coordinator = Coordinator()
    aggregate = coordinator.create_request(
        "R1", "tenant-1", "idem-1", "sha256:req", max_attempts=2
    )
    coordinator.open_attempt("R1", "A1", "res-1", "provider-A")
    assert coordinator.commit_output("R1", "A1", 1, "gen-A1") is True

    with pytest.raises(StateTransitionError) as excinfo:
        coordinator.open_attempt("R1", "B1", "res-2", "provider-B")

    assert "COMMITTED" in str(excinfo.value)
    # The refusal leaves state untouched: no second branch, winner still eligible.
    assert sorted(aggregate.attempts) == ["A1"]
    assert aggregate.current_attempt().attempt_id == "A1"
    assert aggregate.output.attempt_id == "A1"


def test_open_attempt_beyond_budget_is_refused() -> None:
    """The attempt budget is counted against attempts already opened."""
    coordinator = Coordinator()
    aggregate = coordinator.create_request(
        "R1", "tenant-1", "idem-1", "sha256:req", max_attempts=1
    )
    coordinator.open_attempt("R1", "A1", "res-1", "provider-A")

    with pytest.raises(StateTransitionError) as excinfo:
        coordinator.open_attempt("R1", "B1", "res-2", "provider-B")

    assert "budget exhausted (1)" in str(excinfo.value)
    assert sorted(aggregate.attempts) == ["A1"]


def test_first_attempt_on_fresh_request_succeeds() -> None:
    """The first attempt is always admitted and owns output eligibility."""
    coordinator = Coordinator()
    aggregate = coordinator.create_request(
        "R1", "tenant-1", "idem-1", "sha256:req", max_attempts=2
    )

    attempt = coordinator.open_attempt("R1", "A1", "res-1", "provider-A")

    assert attempt.attempt_id == "A1"
    assert attempt.output_eligible is True
    assert attempt.superseded_by is None
    assert aggregate.current_attempt().attempt_id == "A1"
    assert coordinator.commit_output("R1", "A1", 1, "gen-A1") is True


def test_retry_via_handle_attempt_failure_still_works() -> None:
    """A retry opened through the failure path produces one live replacement branch."""
    coordinator = Coordinator()
    aggregate = coordinator.create_request(
        "R1", "tenant-1", "idem-1", "sha256:req", max_attempts=2
    )
    coordinator.open_attempt("R1", "A1", "res-1", "provider-A")

    coordinator.handle_attempt_failure(
        "R1",
        "A1",
        _context(),
        new_attempt_factory=lambda epoch: coordinator.open_attempt(
            "R1", "A2", "res-2", "provider-B"
        ),
    )

    assert sorted(aggregate.attempts) == ["A1", "A2"]
    assert aggregate.attempts["A1"].output_eligible is False
    assert aggregate.attempts["A1"].superseded_by == "A2"
    assert aggregate.current_attempt().attempt_id == "A2"
    # Only the replacement branch can publish.
    assert coordinator.commit_output("R1", "A1", 1, "gen-A1") is False
    assert coordinator.commit_output("R1", "A2", 1, "gen-A2") is True


def test_retry_after_commit_cannot_open_a_second_branch() -> None:
    """The retry path is refused by the same commit guard open_attempt uses.

    The caller-supplied context claims ``output_committed=False``, but the
    aggregate's real state wins: no branch may be spliced behind published output.
    """
    coordinator = Coordinator()
    aggregate = coordinator.create_request(
        "R1", "tenant-1", "idem-1", "sha256:req", max_attempts=3
    )
    coordinator.open_attempt("R1", "A1", "res-1", "provider-A")
    assert coordinator.commit_output("R1", "A1", 1, "gen-A1") is True

    decision = coordinator.handle_attempt_failure(
        "R1",
        "A1",
        _context(output_committed=False),
        new_attempt_factory=lambda epoch: coordinator.open_attempt(
            "R1", "A2", "res-2", "provider-B"
        ),
    )

    assert decision.action == "fail_explicitly"
    assert decision.new_attempt is None
    assert sorted(aggregate.attempts) == ["A1"]
    assert aggregate.output.attempt_id == "A1"


# -- handle_attempt_failure refuses stale or mismatched facts ---------------


def test_failing_superseded_attempt_is_refused() -> None:
    """A superseded attempt's failure must not drive a failover."""
    coordinator = Coordinator()
    aggregate = coordinator.create_request(
        "R1", "tenant-1", "idem-1", "sha256:req", max_attempts=3
    )
    coordinator.open_attempt("R1", "A1", "res-1", "provider-A")
    coordinator.open_attempt("R1", "A2", "res-2", "provider-B")
    assert aggregate.current_attempt().attempt_id == "A2"

    with pytest.raises(StateTransitionError) as excinfo:
        coordinator.handle_attempt_failure(
            "R1",
            "A1",
            _context(),
            new_attempt_factory=lambda epoch: coordinator.open_attempt(
                "R1", "A3", "res-3", "provider-C"
            ),
        )

    assert "not the current output-eligible branch (current: A2)" in str(excinfo.value)
    # No stale-fact failover happened: the live branch is still A2.
    assert sorted(aggregate.attempts) == ["A1", "A2"]
    assert aggregate.current_attempt().attempt_id == "A2"
    assert aggregate.attempts["A2"].superseded_by is None


def test_failing_unknown_attempt_is_refused() -> None:
    """An attempt ID that was never opened cannot be failed."""
    coordinator = Coordinator()
    coordinator.create_request("R1", "tenant-1", "idem-1", "sha256:req", max_attempts=2)
    coordinator.open_attempt("R1", "A1", "res-1", "provider-A")

    with pytest.raises(StateTransitionError) as excinfo:
        coordinator.handle_attempt_failure("R1", "ZZ", _context())

    assert "unknown attempt ZZ" in str(excinfo.value)


def test_failing_attempt_with_mismatched_request_id_is_refused() -> None:
    """A stored attempt stamped with another request ID is refused."""
    coordinator = Coordinator()
    aggregate = coordinator.create_request(
        "R1", "tenant-1", "idem-1", "sha256:req", max_attempts=2
    )
    coordinator.open_attempt("R1", "A1", "res-1", "provider-A")
    aggregate.attempts["A1"] = Attempt(
        attempt_id="A1",
        request_id="R-OTHER",
        resource_id="res-1",
        provider_id="provider-A",
        dispatch_epoch=aggregate.request.dispatch_epoch,
    )

    with pytest.raises(StateTransitionError) as excinfo:
        coordinator.handle_attempt_failure("R1", "A1", _context())

    assert "belongs to request R-OTHER, not R1" in str(excinfo.value)
