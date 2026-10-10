"""Invariants I1-I12 from design section 18.1, plus metering and recovery depth.

These cover the parts of the design that are easy to state and easy to get
subtly wrong: normalization across providers, cumulative versus delta
semantics, parent/child coverage, and the recovery decision table.
"""

from __future__ import annotations

import pytest

from aipotluck.canonical import (
    DuplicateKeyError,
    NonCanonicalNumberError,
    canonical_digest,
    canonicalize,
    encode_signed_payload,
    loads_strict,
    sign_payload,
    verify_payload,
)
from aipotluck.coordinator import Coordinator, InMemoryEventStore, LocalPolicy
from aipotluck.errors import ConflictError, CounterResetError, MeteringError, StateTransitionError
from aipotluck.fake_upstream import (
    anthropic_usage_payload,
    normalize_by_style,
    openai_usage_payload,
)
from aipotluck.metering import (
    Measurement,
    MeasurementStore,
    NormalizedUsage,
    normalize_anthropic_usage,
    normalize_openai_usage,
)
from aipotluck.statemachine import (
    RecoveryContext,
    decide_recovery,
    replay_costs_tokens,
)

# -- canonicalization -------------------------------------------------------


def test_jcs_sorts_keys_and_omits_whitespace() -> None:
    """RFC 8785 ordering is byte-stable regardless of insertion order."""
    assert canonicalize({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    assert canonicalize({"a": 2, "b": 1}) == b'{"a":2,"b":1}'
    assert canonicalize({"x": [1, {"z": 1, "y": 2}]}) == b'{"x":[1,{"y":2,"z":1}]}'


def test_jcs_rejects_duplicate_keys() -> None:
    """Duplicate keys are refused instead of silently resolved."""
    with pytest.raises(DuplicateKeyError):
        loads_strict('{"a":1,"a":2}')


def test_jcs_rejects_floats_and_large_integers() -> None:
    """Floats and out-of-range integers are refused; amounts travel as strings."""
    with pytest.raises(NonCanonicalNumberError):
        loads_strict('{"amount":1.5}')
    with pytest.raises(NonCanonicalNumberError):
        loads_strict('{"n":123456789012345678901234567890}')


def test_domain_separation_blocks_cross_type_replay() -> None:
    """A signature over one record type cannot validate as another type."""
    from conftest import Keyring

    keyring = Keyring()
    payload = {"schema": "x", "amount": 100}
    signature = sign_payload(
        "AIPotluck/settlement/v1", payload, keyring.private["settlement-key-1"]
    )
    public = keyring.registry.get_key("settlement-key-1").public_key()
    assert verify_payload("AIPotluck/settlement/v1", payload, signature, public)
    assert not verify_payload("AIPotluck/usage-report/v1", payload, signature, public)
    assert encode_signed_payload("AIPotluck/settlement/v1", payload).startswith(
        b"AIPotluck/settlement/v1\0"
    )


def test_canonical_digest_is_insertion_order_independent() -> None:
    """The same logical payload always yields the same digest."""
    assert canonical_digest({"a": 1, "b": 2}) == canonical_digest({"b": 2, "a": 1})


# -- normalization (design 6.3) --------------------------------------------


def test_openai_normalization_keeps_details_as_subsets() -> None:
    """Cached and reasoning tokens are details inside the totals, not additions."""
    usage = normalize_openai_usage(
        openai_usage_payload(12_000, 1_000, cached_tokens=8_000, reasoning_tokens=600)
    )
    assert usage.input_total == 12_000
    assert usage.output_total == 1_000
    assert usage.input_cache_read == 8_000
    assert usage.output_reasoning == 600
    assert usage.total_tokens == 13_000


def test_anthropic_normalization_sums_cache_siblings() -> None:
    """Anthropic reports cache tokens beside input_tokens, so they add up."""
    usage = normalize_anthropic_usage(
        anthropic_usage_payload(2_000, 1_000, cache_read=8_000, cache_creation=2_000)
    )
    assert usage.input_total == 12_000
    assert usage.total_tokens == 13_000


def test_detail_larger_than_parent_is_refused() -> None:
    """A subset detail cannot exceed its parent total."""
    with pytest.raises(MeteringError):
        NormalizedUsage(input_total=100, output_total=10, input_cache_read=200)


def test_both_provider_styles_agree_on_total() -> None:
    """The design's worked example: 13,000 tokens, not 21,600."""
    openai = normalize_openai_usage(
        openai_usage_payload(12_000, 1_000, cached_tokens=8_000, reasoning_tokens=600)
    )
    anthropic = normalize_anthropic_usage(
        anthropic_usage_payload(2_000, 1_000, cache_read=8_000, cache_creation=2_000)
    )
    assert openai.total_tokens == anthropic.total_tokens == 13_000


def test_normalize_by_style_rejects_unknown_provider() -> None:
    """An unverified provider style is refused rather than guessed."""
    with pytest.raises(ValueError):
        normalize_by_style("mystery", {"input": 1})


# -- measurement store (design 6.6, 6.7) -----------------------------------


def _measurement(revision: int, total_in: int, total_out: int, **kwargs: object) -> Measurement:
    return Measurement(
        measurement_id="m-1",
        scope="upstream_call",
        semantics="cumulative_snapshot",
        counter_epoch="epoch_1",
        revision=revision,
        usage=NormalizedUsage(input_total=total_in, output_total=total_out),
        attempt_id="A1",
        **kwargs,  # type: ignore[arg-type]
    )


def test_cumulative_snapshots_replace_rather_than_add() -> None:
    """8 → 17 → 31 bills 31, matching the design's dedup rule."""
    store = MeasurementStore()
    for revision, output in ((1, 8), (2, 17), (3, 31)):
        store.record(_measurement(revision, 100, output))
    assert store.get("m-1").total_tokens == 131


def test_identical_duplicate_is_ignored() -> None:
    """Re-sending the same revision with the same content changes nothing."""
    store = MeasurementStore()
    store.record(_measurement(1, 100, 20))
    assert store.record(_measurement(1, 100, 20)) == "ignored_duplicate"
    assert store.record(_measurement(1, 100, 20)) == "ignored_duplicate"


def test_out_of_order_older_revision_does_not_roll_back() -> None:
    """A late lower revision is ignored, not applied."""
    store = MeasurementStore()
    store.record(_measurement(5, 100, 31))
    assert store.record(_measurement(3, 100, 17)) == "ignored_stale"
    assert store.get("m-1").total_tokens == 131


def test_counter_reset_requires_new_epoch() -> None:
    """A shrinking cumulative total without a new epoch is an error."""
    store = MeasurementStore()
    store.record(_measurement(1, 100, 300))
    with pytest.raises(CounterResetError):
        store.record(_measurement(2, 100, 30))


def test_new_counter_epoch_is_accepted() -> None:
    """A documented counter reset with a new epoch is a legitimate revision."""
    store = MeasurementStore()
    store.record(_measurement(5, 100, 300))
    reset = Measurement(
        measurement_id="m-1",
        scope="upstream_call",
        semantics="cumulative_snapshot",
        counter_epoch="epoch_2",
        revision=6,
        usage=NormalizedUsage(input_total=100, output_total=30),
        attempt_id="A1",
    )
    assert store.record(reset) == "replaced"
    assert store.get("m-1").counter_epoch == "epoch_2"


def test_parent_summary_supersedes_children() -> None:
    """1,200 + 800 + a 2,000 parent total bills 2,000, not 4,000."""
    store = MeasurementStore()
    store.record(
        Measurement(
            measurement_id="u1", scope="upstream_call", semantics="delta",
            counter_epoch="e1", revision=1,
            usage=NormalizedUsage(input_total=1_000, output_total=200),
            attempt_id="A1",
        )
    )
    store.record(
        Measurement(
            measurement_id="u2", scope="upstream_call", semantics="delta",
            counter_epoch="e1", revision=1,
            usage=NormalizedUsage(input_total=600, output_total=200),
            attempt_id="A1",
        )
    )
    store.record(
        Measurement(
            measurement_id="u-sum", scope="attempt", semantics="terminal_total",
            counter_epoch="e1", revision=1,
            usage=NormalizedUsage(input_total=1_500, output_total=500),
            attempt_id="A1",
            covered_measurements=frozenset({"u1", "u2"}),
        )
    )
    assert store.non_overlapping_totals("A1") == 2_000
    assert store.superseded_ids("A1") == {"u1", "u2"}


def test_children_without_parent_sum_directly() -> None:
    """Without a parent summary, non-overlapping children are summed."""
    store = MeasurementStore()
    for index, (inp, out) in enumerate(((1_000, 200), (600, 200)), start=1):
        store.record(
            Measurement(
                measurement_id=f"u{index}", scope="upstream_call", semantics="delta",
                counter_epoch="e1", revision=1,
                usage=NormalizedUsage(input_total=inp, output_total=out),
                attempt_id="A1",
            )
        )
    assert store.non_overlapping_totals("A1") == 2_000


def test_overlapping_coverage_is_refused() -> None:
    """Two parents claiming the same child is a double-count risk."""
    store = MeasurementStore()
    for index in (1, 2):
        store.record(
            Measurement(
                measurement_id=f"c{index}", scope="upstream_call", semantics="delta",
                counter_epoch="e1", revision=1,
                usage=NormalizedUsage(input_total=100, output_total=0),
                attempt_id="A1",
            )
        )
    for parent in ("p1", "p2"):
        store.record(
            Measurement(
                measurement_id=parent, scope="attempt", semantics="terminal_total",
                counter_epoch="e1", revision=1,
                usage=NormalizedUsage(input_total=200, output_total=0),
                attempt_id="A1",
                covered_measurements=frozenset({"c1", "c2"}),
            )
        )
    with pytest.raises(MeteringError):
        store.non_overlapping_totals("A1")


# -- recovery decision table (design 11.2) ---------------------------------


def _context(**overrides: object) -> RecoveryContext:
    defaults: dict[str, object] = {
        "attempt_status": "RUNNING",
        "execution_certainty": "submitted",
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


def test_completed_attempt_replays_without_new_tokens() -> None:
    """A stored result is replayed, adding no model usage."""
    action = decide_recovery(_context(attempt_status="COMPLETED"))
    assert action == "replay_result"
    assert not replay_costs_tokens(action)


def test_resumable_execution_is_resumed_not_replaced() -> None:
    """Resuming the same execution avoids a second upstream call."""
    action = decide_recovery(_context(original_execution_resumable=True))
    assert action == "resume_execution"
    assert not replay_costs_tokens(action)


def test_not_submitted_attempt_retries_transparently() -> None:
    """An attempt known not to have executed can move with no waste."""
    action = decide_recovery(
        _context(attempt_status="ACCEPTED", execution_certainty="not_submitted")
    )
    assert action == "retry_on_another_resource"
    assert replay_costs_tokens(action)


def test_buffered_mode_may_retry_before_publishing() -> None:
    """Buffered mode can retry internally because nothing was published."""
    action = decide_recovery(_context(buffered_mode=True))
    assert action == "retry_on_another_resource"


def test_strict_mode_never_double_executes() -> None:
    """Under `deny`, an unverifiable execution is never joined by a second one."""
    action = decide_recovery(_context(duplicate_execution_policy="deny"))
    assert action == "await_recovery"
    assert not replay_costs_tokens(action)


def test_clean_buffered_failure_retries_within_deadline() -> None:
    """A node failure with nothing published and no side effects is retryable.

    This is what makes a real node failure a replacement offer instead of a dead
    request: the consumer only ever sees the eventual result, and the failed
    branch's usage is still recorded.
    """
    action = decide_recovery(_context(attempt_status="FAILED", buffered_mode=True))
    assert action == "retry_on_another_resource"
    assert replay_costs_tokens(action)


def test_committed_output_blocks_retry() -> None:
    """Once output is public, another branch must not be spliced behind it."""
    action = decide_recovery(_context(output_committed=True))
    assert action == "fail_explicitly"


def test_external_side_effects_block_automatic_retry() -> None:
    """Work with outside side effects is never redone automatically."""
    action = decide_recovery(_context(has_external_side_effects=True))
    assert action == "fail_explicitly"


def test_expired_deadline_stops_retrying() -> None:
    """No deadline remaining means no further attempts."""
    action = decide_recovery(_context(deadline_remaining_ms=0))
    assert action == "fail_explicitly"


def test_non_replayable_input_cannot_retry() -> None:
    """Input that cannot be replayed has no retry path."""
    action = decide_recovery(_context(input_replayable=False))
    assert action == "fail_explicitly"


# -- event store and output commit race (design 10.2, 8.3) ----------------


def test_identical_event_replay_is_deduplicated() -> None:
    """At-least-once delivery does not duplicate stored events."""
    events = InMemoryEventStore()
    first = events.append("A1", 1, "output.delta", {"text": "hi"})
    second = events.append("A1", 1, "output.delta", {"text": "hi"})
    assert first == second
    assert events.count("A1") == 1


def test_same_sequence_different_content_is_refused() -> None:
    """Reusing a sequence number for new content is a protocol violation."""
    events = InMemoryEventStore()
    events.append("A1", 1, "output.delta", {"text": "hi"})
    with pytest.raises(ConflictError):
        events.append("A1", 1, "output.delta", {"text": "different"})


def test_sequence_gap_is_detected() -> None:
    """A gap means the stream is incomplete, not merely reordered."""
    events = InMemoryEventStore()
    events.append("A1", 1, "output.delta", {"text": "a"})
    events.append("A1", 2, "output.delta", {"text": "b"})
    events.append("A1", 4, "output.delta", {"text": "d"})
    assert events.contiguous_prefix("A1") == 2
    assert events.has_gap("A1")


def test_only_one_branch_may_commit_output() -> None:
    """A request may have at most one committed output generation."""
    coordinator = Coordinator()
    coordinator.create_request("R1", "tenant-1", "idem-1", "sha256:req")
    coordinator.open_attempt("R1", "A1", "res-1", "provider-A")
    assert coordinator.commit_output("R1", "A1", 1, "gen-A1") is True
    assert coordinator.commit_output("R1", "A1", 2, "gen-A1") is False


def test_superseded_attempt_output_is_refused() -> None:
    """A branch that lost eligibility cannot publish output."""
    coordinator = Coordinator()
    coordinator.create_request("R1", "tenant-1", "idem-1", "sha256:req")
    coordinator.open_attempt("R1", "A1", "res-1", "provider-A")
    coordinator.handle_attempt_failure(
        "R1", "A1", _context(attempt_status="ACCEPTED", execution_certainty="not_submitted"),
        new_attempt_factory=lambda epoch: coordinator.open_attempt(
            "R1", "A2", "res-2", "provider-B"
        ),
    )
    assert coordinator.commit_output("R1", "A1", 1, "gen-A1") is False
    assert coordinator.commit_output("R1", "A2", 1, "gen-A2") is True


def test_failover_stops_at_the_attempt_budget() -> None:
    """The attempt cap bounds retries instead of looping indefinitely."""
    coordinator = Coordinator()
    coordinator.create_request("R1", "tenant-1", "idem-1", "sha256:req", max_attempts=2)
    coordinator.open_attempt("R1", "A1", "res-1", "provider-A")
    coordinator.handle_attempt_failure(
        "R1", "A1", _context(attempt_status="ACCEPTED", execution_certainty="not_submitted"),
        new_attempt_factory=lambda epoch: coordinator.open_attempt(
            "R1", "A2", "res-2", "provider-B"
        ),
    )
    decision = coordinator.handle_attempt_failure(
        "R1", "A2", _context(attempt_status="ACCEPTED", execution_certainty="not_submitted"),
        new_attempt_factory=lambda epoch: coordinator.open_attempt(
            "R1", "A3", "res-3", "provider-C"
        ),
    )
    assert decision.action == "fail_explicitly"


def test_idempotency_key_conflict_is_refused() -> None:
    """Reusing a key with different content is a 409-class conflict."""
    coordinator = Coordinator()
    coordinator.create_request("R1", "tenant-1", "idem-1", "sha256:req-a")
    with pytest.raises(ConflictError):
        coordinator.create_request("R2", "tenant-1", "idem-1", "sha256:req-b")


def test_same_idempotency_key_same_payload_returns_same_request() -> None:
    """A retried request with identical content returns the original."""
    coordinator = Coordinator()
    first = coordinator.create_request("R1", "tenant-1", "idem-1", "sha256:req")
    again = coordinator.create_request("R1-retry", "tenant-1", "idem-1", "sha256:req")
    assert again is first


# -- terminal state is terminal (design 8.1) ------------------------------


def test_terminal_request_does_not_reopen() -> None:
    """A late message cannot move a finished request back to running."""
    coordinator = Coordinator()
    aggregate = coordinator.create_request("R1", "tenant-1", "idem-1", "sha256:req")
    aggregate.request.transition("RESERVED")
    aggregate.request.transition("ROUTING")
    aggregate.request.transition("RUNNING")
    aggregate.request.transition("SUCCEEDED")
    with pytest.raises(StateTransitionError):
        aggregate.request.transition("RUNNING")


def test_cannot_fail_over_after_commit() -> None:
    """After commit, only resumption is allowed — never a different branch."""
    coordinator = Coordinator()
    aggregate = coordinator.create_request("R1", "tenant-1", "idem-1", "sha256:req")
    coordinator.open_attempt("R1", "A1", "res-1", "provider-A")
    coordinator.commit_output("R1", "A1", 1, "gen-A1")

    # Resumable: recovering the original execution is preferred over failing,
    # and still adds no new branch.
    resumable = coordinator.handle_attempt_failure(
        "R1", "A1",
        _context(
            attempt_status="RUNNING",
            output_committed=True,
            original_execution_resumable=True,
        ),
    )
    assert resumable.action == "resume_execution"

    # Not resumable: the only remaining option is an explicit failure.
    unrecoverable = coordinator.handle_attempt_failure(
        "R1", "A1",
        _context(
            attempt_status="RUNNING",
            output_committed=True,
            original_execution_resumable=False,
        ),
    )
    assert unrecoverable.action == "fail_explicitly"
    assert aggregate.output.attempt_id == "A1"


# -- local policy is final (invariant I2) ---------------------------------


def test_paused_device_admits_nothing() -> None:
    """After pausing, the local policy admits zero new executions."""
    policy = LocalPolicy(share_enabled=False)
    admitted, reason = policy.admit(1_000)
    assert not admitted
    assert "paused" in reason


def test_local_concurrency_limit_is_enforced() -> None:
    """The local concurrency cap cannot be raised by a coordinator request."""
    policy = LocalPolicy(max_concurrency=1)
    policy.begin(1_000)
    admitted, reason = policy.admit(1_000)
    assert not admitted
    assert "concurrency" in reason


def test_local_budget_blocks_overspend() -> None:
    """A permit cannot push local exposure past the configured budget."""
    policy = LocalPolicy(max_concurrency=2, max_local_reserved_milli_credits=5_000)
    policy.begin(4_000)
    admitted, reason = policy.admit(2_000)
    assert not admitted
    assert "budget" in reason


def test_best_effort_adapter_can_exceed_reservation() -> None:
    """A best-effort adapter's actual spend may exceed its reservation."""
    policy = LocalPolicy(max_local_reserved_milli_credits=5_000)
    policy.begin(4_000)
    policy.end(9_000)
    assert policy.admit(1_000)[0]