"""Regression tests for the P2 consistency fixes.

Three defects are pinned here:

- F5: a counter-epoch change must not turn a same-revision content conflict
  into stale noise, while a reset with a higher revision must still supersede.
- F7: a parent summary's total is cross-checked against the sum of its declared
  children as a non-raising alarm; the structural duplicate-claim error stays.
- F10b: the evidence ordering is a display choice, not a scope hierarchy.
"""

from __future__ import annotations

import pytest

from aipotluck.errors import MeteringError
from aipotluck.metering import (
    CoverageDiscrepancy,
    Measurement,
    MeasurementStore,
    NormalizedUsage,
)
from aipotluck.verify import _highest_evidence_level, evidence_covers


def _measurement(
    measurement_id: str,
    *,
    counter_epoch: str,
    revision: int,
    input_total: int,
    output_total: int,
    semantics: str = "cumulative_snapshot",
    attempt_id: str = "A1",
    covered: frozenset[str] = frozenset(),
) -> Measurement:
    """Build a measurement with explicit revision, epoch, and token content."""
    return Measurement(
        measurement_id=measurement_id,
        scope="upstream_call",
        semantics=semantics,  # type: ignore[arg-type]
        counter_epoch=counter_epoch,
        revision=revision,
        usage=NormalizedUsage(input_total=input_total, output_total=output_total),
        attempt_id=attempt_id,
        covered_measurements=covered,
    )


# -- F5: counter-epoch conflict semantics ---------------------------------


def test_epoch_change_same_revision_freezes() -> None:
    """Different epoch, same revision, different content freezes the conflict."""
    store = MeasurementStore()
    original = _measurement(
        "m-1", counter_epoch="epoch_1", revision=2, input_total=100, output_total=20
    )
    conflicting = _measurement(
        "m-1", counter_epoch="epoch_2", revision=2, input_total=100, output_total=999
    )
    assert store.record(original) == "applied"
    assert store.record(conflicting) == "conflict_frozen"

    current = store.get("m-1")
    assert current is not None
    assert current.counter_epoch == "epoch_1"
    assert current.revision == 2
    assert current.total_tokens == 120
    assert store.conflicts()["m-1"] == [original, conflicting]


def test_epoch_change_higher_revision_replaces_as_reset() -> None:
    """A new epoch with a higher revision is a legitimate, smaller reset total."""
    store = MeasurementStore()
    store.record(
        _measurement(
            "m-1", counter_epoch="epoch_1", revision=5, input_total=900, output_total=100
        )
    )
    reset = _measurement(
        "m-1", counter_epoch="epoch_2", revision=6, input_total=30, output_total=0
    )
    assert store.record(reset) == "replaced"

    current = store.get("m-1")
    assert current is not None
    assert current.counter_epoch == "epoch_2"
    assert current.revision == 6
    assert current.total_tokens == 30


def test_epoch_change_lower_revision_is_stale() -> None:
    """A late arrival from an old epoch never rolls back the current view."""
    store = MeasurementStore()
    store.record(
        _measurement(
            "m-1", counter_epoch="epoch_1", revision=5, input_total=900, output_total=100
        )
    )
    stale = _measurement(
        "m-1", counter_epoch="epoch_2", revision=3, input_total=1, output_total=1
    )
    assert store.record(stale) == "ignored_stale"

    current = store.get("m-1")
    assert current is not None
    assert current.counter_epoch == "epoch_1"
    assert current.revision == 5
    assert current.total_tokens == 1000


# -- F7: parent/child coverage cross-check --------------------------------


def _store_with_two_children() -> MeasurementStore:
    """Return a store holding children c1 (1,200) and c2 (800) in attempt A1."""
    store = MeasurementStore()
    store.record(
        _measurement(
            "c1",
            counter_epoch="e1",
            revision=1,
            input_total=1000,
            output_total=200,
            semantics="delta",
        )
    )
    store.record(
        _measurement(
            "c2",
            counter_epoch="e1",
            revision=1,
            input_total=600,
            output_total=200,
            semantics="delta",
        )
    )
    return store


def _parent(input_total: int, output_total: int) -> Measurement:
    """Return a parent summary covering c1 and c2."""
    return _measurement(
        "p1",
        counter_epoch="e1",
        revision=1,
        input_total=input_total,
        output_total=output_total,
        semantics="terminal_total",
        covered=frozenset({"c1", "c2"}),
    )


def test_coverage_exact_match_reports_none() -> None:
    """A parent whose total equals its children's sum reports no discrepancy."""
    store = _store_with_two_children()
    store.record(_parent(input_total=1600, output_total=400))
    assert store.coverage_discrepancy("p1") is None


def test_coverage_off_by_n_reports_discrepancy_and_does_not_raise() -> None:
    """A parent off by 50 reports the gap instead of raising."""
    store = _store_with_two_children()
    store.record(_parent(input_total=1650, output_total=400))

    discrepancy = store.coverage_discrepancy("p1")
    assert discrepancy == CoverageDiscrepancy(
        parent_id="p1",
        expected_total=2000,
        declared_total=2050,
        absolute_delta=50,
    )


def test_coverage_tolerance_only_reports_when_exceeded() -> None:
    """The default is exact match; a tolerance admits a difference up to it."""
    store = _store_with_two_children()
    store.record(_parent(input_total=1650, output_total=400))

    reported = store.coverage_discrepancy("p1", tolerance=49)
    assert reported is not None
    assert reported.absolute_delta == 50
    assert store.coverage_discrepancy("p1", tolerance=50) is None


def test_duplicate_child_claim_still_raises() -> None:
    """Two parents claiming one child remains a structural error."""
    store = _store_with_two_children()
    store.record(_parent(input_total=1600, output_total=400))
    store.record(
        _measurement(
            "p2",
            counter_epoch="e1",
            revision=1,
            input_total=1600,
            output_total=400,
            semantics="terminal_total",
            covered=frozenset({"c1", "c2"}),
        )
    )
    with pytest.raises(MeteringError):
        store.non_overlapping_totals("A1")


# -- F10b: evidence ordering is display-only ------------------------------


def test_evidence_order_is_display_only_not_subsumption() -> None:
    """The last display level does not cover a lower level's declared scope."""
    # attested_meter sorts last, so it is displayed as the highest available...
    assert _highest_evidence_level({"platform_observed": 4, "attested_meter": 1}) == (
        "attested_meter"
    )
    # ...yet it does not establish upstream_channel, which upstream_proven does.
    assert evidence_covers("attested_meter", "upstream_channel") is False
    assert evidence_covers("upstream_proven", "upstream_channel") is True
    assert _highest_evidence_level({}) == "unknown"
