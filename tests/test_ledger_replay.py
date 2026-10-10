"""Regression tests for four confirmed ledger defects.

Each defect is reproduced from its report and asserted with concrete balances
and journal identities rather than the absence of an exception:

1. `settle_success` validated funds before checking idempotency, so a replay
   after the payer had spent everything raised `InsufficientFundsError`.
2. `refund` keyed idempotency on the service unit, so a success and its
   correction collided and the second reversal was silently dropped.
3. `_payer_legs` ended with a duplicated, unreachable `return`.
4. `fund_reserve` credited the reserve with no journal, so the movement had no
   audit trail and `total_supply` changed with nothing explaining it.

One test is structural (an AST scan for unreachable statements) because dead
code has no behaviour to assert, and one is a randomized sweep so a fix that
only moves the symptom cannot pass.
"""

from __future__ import annotations

import ast
import random
from pathlib import Path

import pytest

from aipotluck.allocation import AllocationContract
from aipotluck.errors import InsufficientFundsError, LedgerError
from aipotluck.ledger import Journal, Ledger

SOLO_A = AllocationContract(contract_id="solo-a", version=1, weights={"prov-A": 100})
SHARED = AllocationContract(
    contract_id="rand", version=1, weights={"prov-A": 60, "prov-B": 40}
)


def _funded_consumer(amount: int) -> Ledger:
    """Return a ledger with one funded consumer and one provider pending account."""
    book = Ledger()
    book.issue("consumer-1", "consumer_available", amount)
    book.account("prov-A/pending", "provider_pending")
    return book


def _postings(journal: Journal) -> dict[str, int]:
    """Return a journal's postings as an account-to-delta mapping."""
    return {posting.account: posting.delta for posting in journal.postings}


def _unexplained_accounts(book: Ledger, mint_account: str = "treasury/issuance") -> list[str]:
    """Return accounts whose balance is not the sum of the postings that name them.

    `treasury/issuance` is the explicit mint account: `issue` and `fund_reserve`
    top it up so the mint itself is not a posting, so it is excluded. Every other
    account must be fully explained by the journal list.
    """
    unexplained = []
    for account_id, account in book._accounts.items():
        if account_id == mint_account:
            continue
        posted = sum(
            posting.delta
            for journal in book.journals()
            for posting in journal.postings
            if posting.account == account_id
        )
        if account.balance != posted:
            unexplained.append(account_id)
    return unexplained


# -- Defect 1: settle_success checked funds before idempotency --------------


def test_settle_replay_after_full_spend_returns_original_journal() -> None:
    """A replay after the payer spent everything returns J-1 and moves no credit."""
    book = _funded_consumer(1_000)
    first, first_allocation = book.settle_success(
        "S1", "consumer-1", ["prov-A"], 1_000, SOLO_A, "J-1"
    )
    assert first.journal_id == "J-1"
    assert book.balance("consumer-1") == 0
    assert book.balance("prov-A/pending") == 1_000

    # The payer now holds zero; the replay must not attempt another debit.
    second, second_allocation = book.settle_success(
        "S1", "consumer-1", ["prov-A"], 1_000, SOLO_A, "J-2"
    )

    assert second.journal_id == "J-1"
    assert second is first
    assert first_allocation == {"prov-A": 1_000}
    assert second_allocation == {"prov-A": 1_000}
    assert book.balance("consumer-1") == 0
    assert book.balance("prov-A/pending") == 1_000
    assert book.total_supply() == 1_000


def test_correction_replay_returns_the_adjustment_journal_not_the_success() -> None:
    """Replaying a correction returns the adjustment journal, leaving J-1 alone."""
    book = _funded_consumer(5_000)
    success, _ = book.settle_success("S1", "consumer-1", ["prov-A"], 1_000, SOLO_A, "J-1")
    correction, correction_allocation = book.settle_success(
        "S1", "consumer-1", ["prov-A"], 1_500, SOLO_A, "J-2",
        already_recorded={"prov-A": 1_000},
    )
    assert correction.journal_id == "J-2"
    assert correction.kind == "adjustment"
    assert correction_allocation == {"prov-A": 1_500}
    assert book.balance("prov-A/pending") == 1_500
    assert book.balance("consumer-1") == 3_500

    replay, replay_allocation = book.settle_success(
        "S1", "consumer-1", ["prov-A"], 1_500, SOLO_A, "J-3",
        already_recorded={"prov-A": 1_000},
    )
    assert replay.journal_id == "J-2"
    assert replay is correction
    assert replay_allocation == {"prov-A": 1_500}
    assert book.balance("prov-A/pending") == 1_500
    assert book.balance("consumer-1") == 3_500
    assert success.journal_id == "J-1"


def test_success_and_correction_at_one_version_use_distinct_keys() -> None:
    """`kind` (success vs adjustment) is part of the key, so both can coexist."""
    book = _funded_consumer(5_000)
    book.settle_success("S1", "consumer-1", ["prov-A"], 1_000, SOLO_A, "J-1")
    correction, _ = book.settle_success(
        "S1", "consumer-1", ["prov-A"], 1_500, SOLO_A, "J-2",
        already_recorded={"prov-A": 1_000},
    )
    assert correction.kind == "adjustment"
    assert book.balance("prov-A/pending") == 1_500

    # Re-sending the success at the same version returns J-1 and does not undo
    # the correction that shares the service unit and version.
    replay, _ = book.settle_success("S1", "consumer-1", ["prov-A"], 1_000, SOLO_A, "J-9")
    assert replay.journal_id == "J-1"
    assert book.balance("prov-A/pending") == 1_500
    assert book.balance("consumer-1") == 3_500
    assert book.total_supply() == 5_000


# -- Defect 2: the refund key collided across journals ---------------------


def test_refunding_a_correction_is_a_second_reversal_not_the_first() -> None:
    """A correction's refund posts its own reversal; the two share a service unit."""
    book = _funded_consumer(10_000)
    success, _ = book.settle_success("S1", "consumer-1", ["prov-A"], 1_000, SOLO_A, "J-1")
    correction, _ = book.settle_success(
        "S1", "consumer-1", ["prov-A"], 1_500, SOLO_A, "J-2",
        already_recorded={"prov-A": 1_000},
    )
    # Both journals carry service_unit_id "S1", which used to collide.
    assert success.service_unit_id == correction.service_unit_id == "S1"

    first = book.refund(success, "R-1")
    second = book.refund(correction, "R-2")

    assert first.journal_id == "R-1"
    assert second.journal_id == "R-2"
    assert first.journal_id != second.journal_id
    assert first.corrects_journal_id == "J-1"
    assert second.corrects_journal_id == "J-2"
    assert _postings(first) == {"consumer-1": 1_000, "prov-A/pending": -1_000}
    assert _postings(second) == {"consumer-1": 500, "prov-A/pending": -500}
    assert book.balance("consumer-1") == 10_000
    assert book.balance("prov-A/pending") == 0
    assert book.total_supply() == 10_000
    assert _unexplained_accounts(book) == []


def test_refunding_the_same_journal_twice_is_still_idempotent() -> None:
    """Scoping the key to the journal keeps a genuine replay a no-op."""
    book = _funded_consumer(10_000)
    success, _ = book.settle_success("S1", "consumer-1", ["prov-A"], 1_000, SOLO_A, "J-1")
    first = book.refund(success, "R-1")
    second = book.refund(success, "R-2")
    assert first.journal_id == second.journal_id == "R-1"
    assert book.balance("consumer-1") == 10_000
    assert book.balance("prov-A/pending") == 0
    assert book.total_supply() == 10_000


# -- Defect 3: unreachable code in _payer_legs ----------------------------


_TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)


def _unreachable_lines(tree: ast.AST) -> list[int]:
    """Return line numbers of statements that directly follow a terminator."""
    lines: list[int] = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for index in range(len(block) - 1):
                if isinstance(block[index], _TERMINATORS):
                    lines.append(block[index + 1].lineno)
    return lines


def test_ledger_source_has_no_unreachable_statements() -> None:
    """Every statement in ledger.py is reachable; the duplicated return is gone."""
    source = Path(__file__).resolve().parents[1] / "src" / "aipotluck" / "ledger.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    assert _unreachable_lines(tree) == []


# -- Defect 4: fund_reserve minted credit with no journal ------------------


def test_fund_reserve_posts_a_balanced_journal() -> None:
    """Funding the reserve records a balanced journal from the issuance treasury."""
    book = Ledger()
    before = len(book.journals())
    book.fund_reserve(5_000)
    assert len(book.journals()) == before + 1
    journal = book.journals()[-1]
    assert journal.kind == "adjustment"
    assert journal.total_delta() == 0
    assert _postings(journal) == {
        "treasury/issuance": -5_000,
        "platform_risk_fund/available": 5_000,
    }
    assert book.balance("platform_risk_fund/available") == 5_000
    assert book.total_supply() == 5_000


def test_fund_reserve_is_fully_explained_by_postings() -> None:
    """The reserve balance equals its postings; only the mint account is unexplained."""
    book = Ledger()
    book.issue("consumer-1", "consumer_available", 1_000)
    book.fund_reserve(2_000)
    assert _unexplained_accounts(book) == []
    assert book.total_supply() == 3_000


def test_fund_reserve_twice_funds_twice_with_distinct_journals() -> None:
    """Each funding is a distinct event, so funding twice adds twice."""
    book = Ledger()
    book.fund_reserve(1_000)
    book.fund_reserve(2_000)
    assert len(book.journals()) == 2
    assert book.balance("platform_risk_fund/available") == 3_000
    assert book.total_supply() == 3_000
    assert _unexplained_accounts(book) == []


def test_fund_reserve_rejects_a_non_positive_amount() -> None:
    """A zero or negative funding is refused rather than recording an empty journal."""
    book = Ledger()
    for bad in (0, -100):
        with pytest.raises(LedgerError):
            book.fund_reserve(bad)
    assert book.journals() == []
    assert book.total_supply() == 0


# -- randomized settle / correct / refund sweep ----------------------------


def test_randomized_settle_correct_refund_conserves_supply_and_replays_as_noop() -> None:
    """500 random sequences keep supply fixed and replay every settled call as a no-op."""
    rng = random.Random(20261008)
    replay_count = 0

    for case in range(500):
        book = Ledger()
        issued = 50_000
        book.issue("consumer-1", "consumer_available", issued)
        for provider in ("prov-A", "prov-B"):
            book.account(f"{provider}/pending", "provider_pending")
        expected_supply = issued
        if rng.random() < 0.5:
            funded = rng.randint(1, 2_000)
            book.fund_reserve(funded)
            expected_supply += funded
        assert book.total_supply() == expected_supply

        settlements: list[Journal] = []
        recorded: dict[str, int] = {}
        version = 0

        for step in range(rng.randint(1, 8)):
            action = rng.choice(["settle", "correct", "refund", "settle"])
            amount = rng.randint(1, 2_000)

            if action == "refund":
                if not settlements:
                    continue
                original = rng.choice(settlements)
                try:
                    reversal = book.refund(original, f"R-{case}-{step}")
                except (InsufficientFundsError, LedgerError):
                    pass
                else:
                    assert reversal.corrects_journal_id == original.journal_id
                    # The service unit's booked amounts are no longer `recorded`,
                    # so a later correction starts from a clean slate.
                    recorded = {}
            else:
                version += 1
                already = dict(recorded) if (action == "correct" and recorded) else None
                args = {
                    "service_unit_id": "S",
                    "payer_account_id": "consumer-1",
                    "beneficiaries": ["prov-A", "prov-B"],
                    "billable_milli_credits": amount,
                    "contract": SHARED,
                    "journal_id": f"J-{case}-{version}",
                    "version": version,
                    "already_recorded": already,
                }
                try:
                    journal, allocation = book.settle_success(**args)
                except (InsufficientFundsError, LedgerError):
                    continue
                settlements.append(journal)
                recorded = dict(allocation)

                # Replaying the identical call (only the candidate journal id
                # differs) must return the original and touch nothing.
                snapshot = {
                    account_id: book.balance(account_id) for account_id in book._accounts
                }
                supply_before = book.total_supply()
                replay, replay_allocation = book.settle_success(
                    **{**args, "journal_id": f"J-{case}-{version}-replay"}
                )
                assert replay.journal_id == journal.journal_id
                assert replay_allocation == allocation
                assert {
                    account_id: book.balance(account_id) for account_id in book._accounts
                } == snapshot
                assert book.total_supply() == supply_before
                replay_count += 1

            assert book.total_supply() == expected_supply, (case, step, action)

    # The sweep must actually exercise replays for it to prove anything.
    assert replay_count >= 500
