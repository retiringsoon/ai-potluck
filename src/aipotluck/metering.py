"""Token measurement: normalization, deduplication, and coverage.

Why this lives in the client package: normalization and evidence levels are
protocol and verification primitives — a consumer must be able to check what a
receipt claims without trusting the center. Center bookkeeping and the gateway
live in ``ai-potluck-server``, which imports these primitives to keep its own
books.

Three layers are kept apart on purpose. Raw measurements record what an upstream
reported and over what scope. Normalized measurements express a single shape
across providers. Settled usage is a separate decision about who pays.

Two rules are load-bearing. Cumulative snapshots replace rather than add, so
8 → 17 → 31 bills 31, not 56. And a parent summary supersedes its children
instead of stacking on them, so 1,200 + 800 + 2,000 bills 2,000.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast

from .errors import CounterResetError, MeteringError

Scope = Literal["upstream_call", "turn", "attempt", "session", "isolated_task"]
Semantics = Literal["delta", "cumulative_snapshot", "terminal_total"]
Completeness = Literal["complete", "partial", "unknown"]
VerificationLevel = Literal[
    "node_reported",
    "platform_observed",
    "upstream_proven",
    "attested_meter",
]

#: Evidence levels are not a ranking; each declares its own scope and trust root.
EVIDENCE_SCOPE: Mapping[str, frozenset[str]] = {
    "node_reported": frozenset({"reporter_identity", "content_integrity"}),
    "platform_observed": frozenset(
        {"reporter_identity", "content_integrity", "upstream_channel"}
    ),
    "upstream_proven": frozenset(
        {"reporter_identity", "content_integrity", "upstream_channel", "upstream_attestation"}
    ),
    "attested_meter": frozenset(
        {"reporter_identity", "content_integrity", "metering_execution"}
    ),
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MeteringError(message)


@dataclass(frozen=True)
class NormalizedUsage:
    """Provider-independent usage for one measurement scope.

    `input_total` already includes cache reads and cache writes; those are also
    exposed separately as details so a report can show the breakdown without
    ever adding them again.
    """

    input_total: int
    output_total: int
    input_cache_read: int | None = None
    input_cache_write: int | None = None
    output_reasoning: int | None = None
    completeness: Completeness = "complete"
    evidence_origin: str = "upstream_via_connector"
    verification_level: VerificationLevel = "node_reported"

    def __post_init__(self) -> None:
        """Validate that details never exceed their parent totals.

        Raises:
            MeteringError: If a negative value or a detail larger than its
                parent total is supplied.
        """
        _require(self.input_total >= 0, f"input_total must be >= 0, got {self.input_total}")
        _require(self.output_total >= 0, f"output_total must be >= 0, got {self.output_total}")
        for name, detail, parent in (
            ("input_cache_read", self.input_cache_read, self.input_total),
            ("input_cache_write", self.input_cache_write, self.input_total),
            ("output_reasoning", self.output_reasoning, self.output_total),
        ):
            if detail is None:
                continue
            _require(detail >= 0, f"{name} must be >= 0 or None, got {detail}")
            _require(
                detail <= parent,
                f"{name}={detail} exceeds its parent total {parent}; "
                "details are a subset, never additive",
            )

    @property
    def total_tokens(self) -> int:
        """Return input plus output; cached and reasoning tokens are already inside."""
        return self.input_total + self.output_total

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON payload used in signed usage reports."""
        return {
            "input_total": self.input_total,
            "output_total": self.output_total,
            "input_cache_read": self.input_cache_read,
            "input_cache_write": self.input_cache_write,
            "output_reasoning": self.output_reasoning,
            "completeness": self.completeness,
            "evidence_origin": self.evidence_origin,
            "verification_level": self.verification_level,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> NormalizedUsage:
        """Rebuild a `NormalizedUsage` from its signed payload form."""
        return cls(
            input_total=int(payload["input_total"]),
            output_total=int(payload["output_total"]),
            input_cache_read=_optional_int(payload.get("input_cache_read")),
            input_cache_write=_optional_int(payload.get("input_cache_write")),
            output_reasoning=_optional_int(payload.get("output_reasoning")),
            completeness=cast(
                Completeness, str(payload.get("completeness", "complete"))
            ),
            evidence_origin=str(payload.get("evidence_origin", "upstream_via_connector")),
            verification_level=cast(
                VerificationLevel,
                str(payload.get("verification_level", "node_reported")),
            ),
        )


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def normalize_openai_usage(raw: Mapping[str, Any]) -> NormalizedUsage:
    """Normalize an OpenAI Chat Completions usage object.

    `prompt_tokens` and `completion_tokens` are the totals. Cached and reasoning
    counts are subsets of them and are reported as details only.

    Args:
        raw: The upstream `usage` mapping.

    Returns:
        The normalized usage.

    Raises:
        MeteringError: If the required fields are missing or non-integer.
    """
    for field_name in ("prompt_tokens", "completion_tokens"):
        if field_name not in raw:
            raise MeteringError(f"OpenAI usage is missing {field_name}")
    details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    return NormalizedUsage(
        input_total=int(raw["prompt_tokens"]),
        output_total=int(raw["completion_tokens"]),
        input_cache_read=_optional_int(details.get("cached_tokens")),
        output_reasoning=_optional_int(completion_details.get("reasoning_tokens")),
        completeness="complete",
    )


def normalize_anthropic_usage(raw: Mapping[str, Any]) -> NormalizedUsage:
    """Normalize an Anthropic Messages usage object.

    Anthropic reports cache tokens as siblings of `input_tokens`, so the input
    total is their sum. Further cache-write subdivisions are details of
    `cache_creation_input_tokens` and must not be added again.

    Args:
        raw: The upstream `usage` mapping.

    Returns:
        The normalized usage.

    Raises:
        MeteringError: If `input_tokens` or `output_tokens` is missing.
    """
    for field_name in ("input_tokens", "output_tokens"):
        if field_name not in raw:
            raise MeteringError(f"Anthropic usage is missing {field_name}")
    cache_read = int(raw.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(raw.get("cache_creation_input_tokens", 0) or 0)
    plain_input = int(raw["input_tokens"])
    return NormalizedUsage(
        input_total=plain_input + cache_read + cache_write,
        output_total=int(raw["output_tokens"]),
        input_cache_read=cache_read or None,
        input_cache_write=cache_write or None,
    )


@dataclass
class Measurement:
    """One measurement record: a scope, a revision, and a normalization.

    Revisions replace the current view rather than accumulate. A higher revision
    supersedes a lower one; a lower revision arriving late is ignored; a
    cumulative value that shrinks without a new `counter_epoch` is an explicit
    error rather than something to paper over by taking the maximum.
    """

    measurement_id: str
    scope: Scope
    semantics: Semantics
    counter_epoch: str
    revision: int
    usage: NormalizedUsage
    attempt_id: str
    upstream_call_id: str | None = None
    adapter_version: str = "0.1.0"
    covered_measurements: frozenset[str] = field(default_factory=frozenset)

    @property
    def total_tokens(self) -> int:
        """Return the normalized total for this measurement."""
        return self.usage.total_tokens

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON payload bound by a contributor's signature."""
        return {
            "measurement_id": self.measurement_id,
            "attempt_id": self.attempt_id,
            "upstream_call_id": self.upstream_call_id,
            "scope": self.scope,
            "semantics": self.semantics,
            "counter_epoch": self.counter_epoch,
            "revision": self.revision,
            "covered_measurements": sorted(self.covered_measurements),
            "adapter_version": self.adapter_version,
            "usage": self.usage.to_payload(),
        }


@dataclass(frozen=True)
class CoverageDiscrepancy:
    """A parent summary whose total disagrees with the children it declares.

    This is an alarm, not an error. The design keeps both sides' evidence: the
    parent's own total and the sum of its declared children are surfaced
    together so a caller can log the gap, rather than the settlement path
    refusing to proceed when the numbers happen to differ.

    Attributes:
        parent_id: The measurement ID of the parent summary.
        expected_total: The sum of the totals of the children it declares.
        declared_total: The total the parent summary itself reports.
        absolute_delta: ``abs(declared_total - expected_total)``.
    """

    parent_id: str
    expected_total: int
    declared_total: int
    absolute_delta: int


class MeasurementStore:
    """Deduplicating store for measurements, scoped by attempt.

    Holds the current revision per `measurement_id` plus the frozen set of
    conflicting payloads, so a same-ID-different-content conflict is visible
    instead of silently resolved.
    """

    def __init__(self) -> None:
        self._current: dict[str, Measurement] = {}
        self._conflicts: dict[str, list[Measurement]] = {}

    def record(self, measurement: Measurement) -> str:
        """Apply a measurement under the deduplication rules.

        Conflict and staleness are judged by revision. The `counter_epoch` only
        changes one of those judgements, and it does so deliberately:

        * A content conflict at the **same revision** always freezes, in the
          same epoch or in a new one. Two different payloads cannot both be the
          revision-`n` value, so both are kept and `conflict_frozen` is
          returned regardless of `counter_epoch`.
        * A **higher** revision in a **new epoch** is a legitimate `replaced`.
          A new epoch means the upstream counter was reset, and a reset
          supersedes the value it follows, so the incoming total is applied
          without a content conflict check and without a `CounterResetError`.
          This is the one place an epoch change carries meaning rather than
          merely being a field that differs.
        * A **lower** revision in a new epoch is still `ignored_stale`; a late
          arrival from an old epoch never rolls back the current view.

        Within a single epoch a higher revision still supersedes a lower one,
        and a shrinking cumulative snapshot still raises `CounterResetError`.

        Args:
            measurement: The incoming measurement.

        Returns:
            One of `applied`, `replaced`, `ignored_stale`, `ignored_duplicate`,
            or `conflict_frozen`.

        Raises:
            CounterResetError: If a cumulative value decreases within the same
                `counter_epoch`.
            MeteringError: If a revision is below 1.
        """
        if measurement.revision < 1:
            raise MeteringError(f"revision must be >= 1, got {measurement.revision}")

        key = measurement.measurement_id
        existing = self._current.get(key)
        if existing is None:
            self._current[key] = measurement
            return "applied"

        # A lower revision is stale no matter which epoch it arrives in. The
        # epoch is part of the payload, so an identical payload always shares
        # the epoch and revision and can be caught as a duplicate below.
        if measurement.revision < existing.revision:
            return "ignored_stale"

        if existing.to_payload() == measurement.to_payload():
            return "ignored_duplicate"

        # Same revision, different content: freeze in every epoch. An epoch
        # change does not turn a genuine conflict into a silently dropped one.
        if measurement.revision == existing.revision:
            self._conflicts.setdefault(key, [existing]).append(measurement)
            return "conflict_frozen"

        # Higher revision. A new epoch means the counter was reset, so the
        # reset legitimately supersedes the previous value with no further
        # checks; only a same-epoch supersession has to justify a non-shrink.
        if existing.counter_epoch != measurement.counter_epoch:
            self._current[key] = measurement
            return "replaced"

        if (
            measurement.semantics == "cumulative_snapshot"
            and measurement.usage.total_tokens < existing.usage.total_tokens
        ):
            raise CounterResetError(
                f"measurement {key} cumulative total fell from "
                f"{existing.usage.total_tokens} to {measurement.usage.total_tokens} "
                f"within counter_epoch {measurement.counter_epoch}; "
                "a reset requires a new counter_epoch"
            )

        self._current[key] = measurement
        return "replaced"

    def get(self, measurement_id: str) -> Measurement | None:
        """Return the current revision, or None when unseen."""
        return self._current.get(measurement_id)

    def conflicts(self) -> dict[str, list[Measurement]]:
        """Return frozen conflicts keyed by measurement ID."""
        return dict(self._conflicts)

    def attempts(self) -> dict[str, list[Measurement]]:
        """Group current measurements by attempt ID.

        Each attempt is summed from a non-overlapping set: a measurement that
        covers children replaces them rather than stacking with them.
        """
        grouped: dict[str, list[Measurement]] = {}
        for measurement in self._current.values():
            grouped.setdefault(measurement.attempt_id, []).append(measurement)
        return grouped

    def non_overlapping_totals(self, attempt_id: str) -> int:
        """Sum an attempt's usage without double counting parent and child.

        A measurement whose `covered_measurements` names other measurements in
        the same attempt replaces them. Coverage sets must be disjoint.

        Args:
            attempt_id: The attempt to total.

        Returns:
            Total tokens across the non-overlapping set.

        Raises:
            MeteringError: If two measurements claim the same covered child.
        """
        measurements = self.attempts().get(attempt_id, [])
        covered: dict[str, str] = {}
        for measurement in measurements:
            for child in measurement.covered_measurements:
                if child in covered and covered[child] != measurement.measurement_id:
                    raise MeteringError(
                        f"measurement {child} is claimed by both {covered[child]} "
                        f"and {measurement.measurement_id}"
                    )
                covered[child] = measurement.measurement_id
        superseded = set(covered)
        return sum(
            m.total_tokens for m in measurements if m.measurement_id not in superseded
        )

    def superseded_ids(self, attempt_id: str) -> set[str]:
        """Return measurement IDs that a parent summary replaces."""
        measurements = self.attempts().get(attempt_id, [])
        covered: set[str] = set()
        for measurement in measurements:
            covered |= measurement.covered_measurements
        return covered

    def coverage_discrepancy(
        self, parent_id: str, *, tolerance: int = 0
    ) -> CoverageDiscrepancy | None:
        """Compare a parent summary's total with the children it declares.

        A parent that declares `covered_measurements` claims to stand in for
        them, so its total should equal the sum of their totals. It does not
        always: the parent's reporter and the children's reporters are
        different parties, and the design keeps both sides' evidence and raises
        an alarm rather than choosing one. This method computes that alarm.

        This is a numeric check, so it never raises on a mismatch; only the
        structural violation of two parents claiming one child raises, and that
        is enforced by `non_overlapping_totals`. A measurement that declares no
        children is not a parent and is reported as no discrepancy. Declared
        children that are not currently held contribute zero, so a parent naming
        an unknown child is reported as a discrepancy rather than crashing the
        settlement path.

        Args:
            parent_id: The measurement ID of the parent summary to check.
            tolerance: The largest absolute difference still treated as a
                match. Defaults to 0, an exact match, because token counts are
                exact integers: the parent is either the children's sum or it is
                not, and a nonzero tolerance would quietly absorb a real
                accounting bug.

        Returns:
            A `CoverageDiscrepancy` when the absolute difference between the
            parent's declared total and the sum of its declared children
            exceeds `tolerance`, otherwise None.

        Raises:
            MeteringError: If no measurement with `parent_id` is held, since
                there is nothing to compare.
        """
        parent = self._current.get(parent_id)
        if parent is None:
            raise MeteringError(
                f"no measurement {parent_id} is held to check coverage against"
            )
        if not parent.covered_measurements:
            return None
        held_children = [
            self._current[child_id]
            for child_id in sorted(parent.covered_measurements)
            if child_id in self._current
        ]
        expected = sum(child.total_tokens for child in held_children)
        declared = parent.total_tokens
        delta = abs(declared - expected)
        if delta <= tolerance:
            return None
        return CoverageDiscrepancy(
            parent_id=parent_id,
            expected_total=expected,
            declared_total=declared,
            absolute_delta=delta,
        )


def apply_cumulative_snapshots(reports: list[Mapping[str, Any]]) -> int:
    """Collapse a stream of cumulative snapshots into the final total.

    Args:
        reports: Successive usage snapshots, each with `input_total` and
            `output_total`.

    Returns:
        The last snapshot's total.

    Raises:
        MeteringError: If the list is empty.
    """
    if not reports:
        raise MeteringError("at least one snapshot is required")
    last = reports[-1]
    return int(last["input_total"]) + int(last["output_total"])


def provisional_to_complete(measurement: Measurement) -> Measurement:
    """Return a copy marked complete, used when a terminal total arrives."""
    return replace(measurement, usage=replace(measurement.usage, completeness="complete"))