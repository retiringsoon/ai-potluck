"""Reservations must actually gate spending, and each risk needs its own bucket.

The v0.3 design review (section 3.1, F3 and F4) found that `reserve` moved credit
into a bucket the settlement path never read, so a reservation did not actually
prevent the money from being spent elsewhere; and that a provider-side hold was
recorded with a consumer account kind, collapsing two different risks into one
pool. Both are guarded here.
"""

from __future__ import annotations

from aipotluck.allocation import AllocationContract
from aipotluck.ledger import Ledger


def _contract(provider: str = "provider-A") -> AllocationContract:
    return AllocationContract(contract_id="c", version=1, weights={provider: 100})


def test_settlement_draws_from_the_reserved_balance_first() -> None:
    """A held reservation must be consumed by settling, not left dangling.

    Charging the free balance first would let a consumer spend credit that is
    already earmarked while the reservation is outstanding.
    """
    ledger = Ledger()
    ledger.issue("consumer-1", "consumer_available", 1_000)
    ledger.account("provider-A/pending", "provider_pending")
    ledger.reserve("res-1", "consumer-1", "consumer_available", 400)

    ledger.settle_success("S1", "consumer-1", ["provider-A"], 400, _contract(), "J-1")

    assert ledger.balance("consumer-1/reserved") == 0
    assert ledger.balance("consumer-1") == 600
    assert ledger.balance("provider-A/pending") == 400
    assert ledger.total_supply() == 1_000


def test_settlement_falls_back_to_available_beyond_the_reservation() -> None:
    """Charging more than was reserved still works, drawing the rest as usual."""
    ledger = Ledger()
    ledger.issue("consumer-1", "consumer_available", 1_000)
    ledger.account("provider-A/pending", "provider_pending")
    ledger.reserve("res-1", "consumer-1", "consumer_available", 400)

    ledger.settle_success("S1", "consumer-1", ["provider-A"], 700, _contract(), "J-1")

    assert ledger.balance("consumer-1/reserved") == 0
    assert ledger.balance("consumer-1") == 300
    assert ledger.total_supply() == 1_000


def test_reserved_credit_is_spent_from_reserved_not_available() -> None:
    """An unrelated settlement charges the reservation, not the free balance.

    With 800 of 1,000 reserved, settling 200 must come out of the reserved
    bucket. If it came from the free balance instead, the reservation would not
    constrain anything and the two buckets would be interchangeable.
    """
    ledger = Ledger()
    ledger.issue("consumer-1", "consumer_available", 1_000)
    ledger.account("provider-A/pending", "provider_pending")
    ledger.account("provider-B/pending", "provider_pending")
    ledger.reserve("res-1", "consumer-1", "consumer_available", 800)

    ledger.settle_success(
        "S-other", "consumer-1", ["provider-B"], 200, _contract("provider-B"), "J-1"
    )

    assert ledger.balance("consumer-1/reserved") == 600
    # The free 200 was never touched, because the charge came from the reserve.
    assert ledger.balance("consumer-1") == 200
    assert ledger.total_supply() == 1_000


def test_provider_reservation_uses_its_own_account_kind() -> None:
    """A provider-side hold must not be recorded as a consumer hold.

    The design keeps three reservation families apart: a consumer's spendable
    credit, a node's local risk exposure, and the platform's reserve. Sharing one
    bucket would let a node's risk cap mask a consumer's hold.
    """
    ledger = Ledger()
    ledger.issue("provider-A", "provider_pending", 20_000)
    ledger.reserve("res-a", "provider-A", "provider_pending", 5_000)

    statement = ledger.statement("provider-A")
    assert statement["risk_reserved"] == 5_000
    assert statement["reserved"] == 0


def test_consumer_and_provider_reservations_are_separate() -> None:
    """A consumer hold and a provider hold stay distinct even for one identity.

    The design keeps a consumer's spendable credit, a node's local risk
    exposure, and the platform's reserve in separate families. An identity may
    act as both consumer and provider, so both holds can exist at once and must
    not be pooled.
    """
    ledger = Ledger()
    # The consumer side is the root account; the provider side is a sub-account.
    ledger.issue("provider-A", "consumer_available", 10_000)
    ledger.issue("provider-A/pending", "provider_pending", 10_000)

    ledger.reserve("res-consumer", "provider-A", "consumer_available", 3_000)
    ledger.reserve("res-node", "provider-A/pending", "provider_pending", 4_000)

    assert ledger.balance("provider-A/reserved") == 3_000
    assert ledger.balance("provider-A/pending/risk_reserved") == 4_000
    assert ledger.balance("provider-A/pending") == 6_000
    assert ledger.total_supply() == 20_000