"""Deterministic integer allocation across multiple beneficiaries.

The contract is fixed before dispatch, so allocation is a pure function of
(total, contract). Two parties running this code on different machines must
reach identical numbers, which is why every step uses integer arithmetic.

Remainder handling follows the design rule exactly: floor division first, then
at most one extra unit per beneficiary, awarded by descending remainder and
broken by ascending beneficiary ID. Zero-weight beneficiaries never receive a
remainder unit, and the total always sums back to the input.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .errors import AllocationError

#: One credit is 1,000 milli-credits; the smallest tradable unit.
MILLI_PER_CREDIT = 1000


def floor_allocate(total: int, weights: Mapping[str, int]) -> dict[str, int]:
    """Split `total` proportionally to `weights` with no lost units.

    Args:
        total: Non-negative amount in milli-credits.
        weights: Beneficiary ID to non-negative weight.

    Returns:
        Beneficiary ID to allocated amount; keys match `weights`, and the values
        sum exactly to `total`.

    Raises:
        AllocationError: If `total` is negative, weights are empty, any weight is
            negative, or the weight sum is zero.
    """
    if total < 0:
        raise AllocationError(f"total must be non-negative, got {total}")
    if not weights:
        raise AllocationError("allocation requires at least one beneficiary")
    negative = [bid for bid, weight in weights.items() if weight < 0]
    if negative:
        raise AllocationError(f"weights must be non-negative, got negative for {sorted(negative)}")

    weight_sum = sum(weights.values())
    if weight_sum <= 0:
        raise AllocationError("sum of weights must be greater than zero")

    return {
        bid: (total * weight) // weight_sum
        for bid, weight in weights.items()
    }


def allocate_with_remainder(total: int, weights: Mapping[str, int]) -> dict[str, int]:
    """Split `total` proportionally, distributing leftover units deterministically.

    Each beneficiary gets `floor(total * w / W)`. The leftover units are handed
    out one apiece to the largest `remainder = (total * w) % W` first; ties break
    on ascending beneficiary ID. Beneficiaries with weight zero are excluded
    from remainders even when their remainder is numerically large.

    Args:
        total: Non-negative amount in milli-credits.
        weights: Beneficiary ID to non-negative weight.

    Returns:
        Beneficiary ID to allocated amount summing exactly to `total`.

    Raises:
        AllocationError: If the weights are empty, negative, or sum to zero.
    """
    base = floor_allocate(total, weights)
    weight_sum = sum(weights.values())
    allocated = sum(base.values())
    remaining = total - allocated

    remainders = {
        bid: (total * weight) % weight_sum
        for bid, weight in weights.items()
        if weight > 0
    }
    ordered = sorted(remainders.items(), key=lambda item: (-item[1], item[0]))

    result = dict(base)
    for beneficiary, _ in ordered[:remaining]:
        result[beneficiary] += 1
    return result


@dataclass(frozen=True)
class AllocationContract:
    """Beneficiary weights plus rounding rules, fixed before dispatch.

    Changing weights never rewrites history: a new contract version applies only
    to authorizations issued after it, and corrections go through
    `AllocationContract` plus an explicit adjustment journal.
    """

    contract_id: str
    version: int
    weights: Mapping[str, int]
    platform_fee_weight: int = 0
    platform_account: str | None = None
    rounding_rule: str = "floor_then_largest_remainder_v1"

    def __post_init__(self) -> None:
        """Validate the contract at construction time.

        Raises:
            AllocationError: On duplicate beneficiaries implied by empty IDs,
                non-positive version, or a platform fee without an account.
        """
        if self.version < 1:
            raise AllocationError(f"contract version must be >= 1, got {self.version}")
        if not self.weights:
            raise AllocationError("allocation contract requires at least one beneficiary")
        bad = [bid for bid in self.weights if not bid]
        if bad:
            raise AllocationError("beneficiary IDs must be non-empty")
        negative = [bid for bid, w in self.weights.items() if w < 0]
        if negative:
            raise AllocationError(
                f"weights must be non-negative, got negative for {sorted(negative)}"
            )
        if self.platform_fee_weight < 0:
            raise AllocationError(
                f"platform_fee_weight must be non-negative, got {self.platform_fee_weight}"
            )
        if self.platform_fee_weight > 0:
            if self.platform_account is None:
                raise AllocationError("a non-zero platform fee requires a platform_account")
            if self.platform_account in self.weights:
                raise AllocationError(
                    f"platform account {self.platform_account!r} must not also be a beneficiary"
                )

    def all_weights(self) -> dict[str, int]:
        """Return beneficiary weights plus the platform fee as one weight set."""
        weights = dict(self.weights)
        if self.platform_fee_weight > 0:
            assert self.platform_account is not None  # guaranteed in __post_init__
            weights[self.platform_account] = self.platform_fee_weight
        return weights

    def distribute(self, total: int) -> dict[str, int]:
        """Allocate `total` milli-credits under this contract.

        Args:
            total: Non-negative amount in milli-credits.

        Returns:
            Beneficiary (or platform) account to amount, summing exactly to `total`.
        """
        return allocate_with_remainder(total, self.all_weights())

    def digest_payload(self) -> dict[str, object]:
        """Return the canonical contract body for hashing and signing."""
        return {
            "contract_id": self.contract_id,
            "version": self.version,
            "weights": dict(sorted(self.weights.items())),
            "platform_fee_weight": self.platform_fee_weight,
            "platform_account": self.platform_account,
            "rounding_rule": self.rounding_rule,
        }


def net_adjustment(
    previous_total: int,
    new_total: int,
    contract: AllocationContract,
    already_recorded: Mapping[str, int],
) -> dict[str, int]:
    """Compute the incremental deltas when a billed total is corrected.

    The full allocation is recomputed against `new_total`, then reduced by what
    each beneficiary has already been credited. A revision therefore posts only
    the difference, never the whole amount again — allocating the extra unit on
    its own is not equivalent to reallocating the full sum.

    `previous_total` is retained as a cross-check rather than as an input to the
    arithmetic: the deltas are derived entirely from `new_total` and
    `already_recorded`. It is verified against `sum(already_recorded.values())`
    so a caller cannot pass a `previous_total` that contradicts the per-beneficiary
    record it settled alongside. Silently ignoring it would let two callers
    disagreeing about the prior total produce different books from the same
    inputs while believing they reconciled.

    Args:
        previous_total: The total previously settled. Must equal the sum of
            `already_recorded`.
        new_total: The corrected total.
        contract: The allocation contract in force.
        already_recorded: Beneficiary to amount already booked.

    Returns:
        Beneficiary to signed delta; only non-zero entries appear, and the
        deltas sum to `new_total - sum(already_recorded.values())`, which equals
        `new_total - previous_total` because the two are verified equal.

    Raises:
        AllocationError: If `new_total` is negative, `already_recorded` covers an
            account the contract does not distribute to, or `previous_total`
            disagrees with the sum of `already_recorded`.
    """
    if new_total < 0:
        raise AllocationError(f"new_total must be non-negative, got {new_total}")
    if previous_total < 0:
        raise AllocationError(f"previous_total must be non-negative, got {previous_total}")
    unknown = set(already_recorded) - set(contract.all_weights())
    if unknown:
        raise AllocationError(
            f"already_recorded names accounts absent from the contract: {sorted(unknown)}"
        )
    recorded_total = sum(already_recorded.values())
    if previous_total != recorded_total:
        raise AllocationError(
            f"previous_total {previous_total} disagrees with already_recorded "
            f"sum {recorded_total}"
        )
    target = contract.distribute(new_total)
    deltas = {
        account: target.get(account, 0) - already_recorded.get(account, 0)
        for account in set(target) | set(already_recorded)
    }
    return {account: delta for account, delta in deltas.items() if delta != 0}


def format_credit(milli_credits: int) -> str:
    """Render milli-credits as a human-readable credit string.

    Args:
        milli_credits: Amount in milli-credits.

    Returns:
        For example `8.7` for 8,700 milli-credits.
    """
    sign = "-" if milli_credits < 0 else ""
    whole, remainder = divmod(abs(milli_credits), MILLI_PER_CREDIT)
    if remainder == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{remainder:03d}".rstrip("0")


def sum_allocations(allocations: Iterable[Mapping[str, int]]) -> int:
    """Sum every allocation's values into one total."""
    return sum(value for allocation in allocations for value in allocation.values())