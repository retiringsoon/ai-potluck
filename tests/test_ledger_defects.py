"""Regression tests for five confirmed money-handling defects in the ledger.

Each defect has a test built from its reproduction script, asserting concrete
numbers rather than the absence of an exception. Three of the tests are
property-style: a randomized supply-conservation sweep, an idempotency sweep
over every public money-moving operation, and a correction sweep. They exist
because the defects shared a root cause — money moved without a matching
liability — which a single example cannot rule out.
"""

from __future__ import annotations

import random

import pytest

from aipotluck.allocation import AllocationContract, net_adjustment
from aipotluck.errors import AllocationError, InsufficientFundsError, LedgerError
from aipotluck.ledger import Journal, Ledger, Posting

CONTRACT = AllocationContract(contract_id="c", version=1, weights={"prov-A": 80, "prov-B": 20})
SOLO_A = AllocationContract(contract_id="solo-a", version=1, weights={"prov-A": 100})
SOLO_B = AllocationContract(contract_id="solo-b", version=1, weights={"prov-B": 100})


def _funded(amount: int = 10_000) -> Ledger:
    """Return a ledger with one consumer and both provider pending accounts."""
    book = Ledger()
    book.issue("consumer-1", "consumer_available", amount)
    for provider in ("prov-A", "prov-B"):
        book.account(f"{provider}/pending", "provider_pending")
    return book


def _assert_reconciles(book: Ledger) -> None:
    """Assert every journal-backed balance equals the sum of its postings.

    `issue` and `fund_reserve` deliberately mint outside a posting — issuance
    tops up `treasury/issuance` and `fund_reserve` credits the risk account
    directly — so those two accounts are excluded and checked through
    `total_supply` instead. Every other account must be fully explained by the
    journal list; that is the property a partially applied journal breaks.
    """
    unposted = {"treasury/issuance", "platform_risk_fund/available"}
    for account_id, account in book._accounts.items():
        if account_id in unposted:
            continue
        posted = sum(
            posting.delta
            for journal in book.journals()
            for posting in journal.postings
            if posting.account == account_id
        )
        assert account.balance == posted, (
            f"{account_id} holds {account.balance} but journals explain {posted}"
        )


def _pending_and_available(book: Ledger, *providers: str) -> None:
    """Register the settlement target accounts an unfreeze will post to."""
    for provider in providers:
        book.account(f"{provider}/pending", "provider_pending")
        book.account(f"{provider}/available", "provider_available")


# -- DEFECT 1: the correction path re-posted the whole amount ---------------


def test_defect1_correction_posts_only_the_net_difference() -> None:
    """A corrected settlement books the difference, never the whole amount again."""
    book = _funded()
    book.settle_success("S1", "consumer-1", ["prov-A", "prov-B"], 1_000, CONTRACT, "J-1")
    book.settle_success(
        "S1", "consumer-1", ["prov-A", "prov-B"], 1_500, CONTRACT, "J-2",
        already_recorded={"prov-A": 800, "prov-B": 200},
    )
    # 80/20 of 1500 is 1200/300, so the correction moves only +400/+100.
    assert book.balance("prov-A/pending") == 1_200
    assert book.balance("prov-B/pending") == 300
    # The consumer paid 1000 then only the 500 difference, never 1500 twice.
    assert book.balance("consumer-1") == 8_500
    # The balanced journal hid the over-credit; total supply is the real check.
    assert book.total_supply() == 10_000


def test_defect1_downward_correction_claws_back_over_credit() -> None:
    """A correction that lowers the total debits the payer and the beneficiaries."""
    book = _funded()
    book.settle_success("S1", "consumer-1", ["prov-A", "prov-B"], 1_500, CONTRACT, "J-1")
    book.settle_success(
        "S1", "consumer-1", ["prov-A", "prov-B"], 1_000, CONTRACT, "J-2",
        already_recorded={"prov-A": 1_200, "prov-B": 300},
    )
    assert book.balance("prov-A/pending") == 800
    assert book.balance("prov-B/pending") == 200
    assert book.balance("consumer-1") == 9_000
    assert book.total_supply() == 10_000


def test_defect1_repeated_correction_within_one_version_moves_nothing() -> None:
    """Re-sending the same correction is a no-op, not a second adjustment."""
    book = _funded()
    book.settle_success("S1", "consumer-1", ["prov-A", "prov-B"], 1_000, CONTRACT, "J-1")
    first, _ = book.settle_success(
        "S1", "consumer-1", ["prov-A", "prov-B"], 1_500, CONTRACT, "J-2",
        already_recorded={"prov-A": 800, "prov-B": 200},
    )
    second, _ = book.settle_success(
        "S1", "consumer-1", ["prov-A", "prov-B"], 1_500, CONTRACT, "J-3",
        already_recorded={"prov-A": 800, "prov-B": 200},
    )
    assert first.journal_id == second.journal_id
    assert book.balance("prov-A/pending") == 1_200
    assert book.balance("prov-B/pending") == 300
    assert book.balance("consumer-1") == 8_500
    assert book.total_supply() == 10_000


def test_defect1_correction_journal_carries_only_the_delta() -> None:
    """The correction journal's postings sum to the difference, not the total."""
    book = _funded()
    book.settle_success("S1", "consumer-1", ["prov-A", "prov-B"], 1_000, CONTRACT, "J-1")
    book.settle_success(
        "S1", "consumer-1", ["prov-A", "prov-B"], 1_500, CONTRACT, "J-2",
        already_recorded={"prov-A": 800, "prov-B": 200},
    )
    correction = book.journal("J-2")
    assert correction is not None
    assert correction.kind == "adjustment"
    assert correction.total_delta() == 0
    amounts = {p.account: p.delta for p in correction.postings}
    assert amounts == {
        "consumer-1": -500,
        "prov-A/pending": 400,
        "prov-B/pending": 100,
    }


# -- DEFECT 2: _post was not atomic ----------------------------------------


def test_defect2_rejected_journal_leaves_every_balance_untouched() -> None:
    """When a later leg overdraws, no earlier leg may stay applied."""
    book = Ledger()
    book.issue("a", "consumer_available", 100)
    book.issue("b", "consumer_available", 100)
    journals_before = len(book.journals())
    with pytest.raises(InsufficientFundsError):
        book.post(
            Journal("X", ("k", "x", 1), "adjustment",
                    postings=(Posting("a", 200), Posting("b", -200)))
        )
    # The credit to "a" would have leaked had the postings applied in order.
    assert book.balance("a") == 100
    assert book.balance("b") == 100
    assert book.journal("X") is None
    assert len(book.journals()) == journals_before
    assert book.total_supply() == 200


def test_defect2_rejected_journal_does_not_consume_its_idempotency_key() -> None:
    """A key burned by a rejected journal is still usable by a valid retry."""
    book = Ledger()
    book.issue("a", "consumer_available", 100)
    book.issue("b", "consumer_available", 100)
    with pytest.raises(InsufficientFundsError):
        book.post(
            Journal("X", ("k", "x", 1), "adjustment",
                    postings=(Posting("a", 200), Posting("b", -200)))
        )
    assert not book.has_idempotency_key(("k", "x", 1))
    book.post(
        Journal("X", ("k", "x", 1), "adjustment",
                postings=(Posting("a", 50), Posting("b", -50)))
    )
    assert book.balance("a") == 150
    assert book.balance("b") == 50


def test_defect2_netted_debits_are_validated_against_the_final_balance() -> None:
    """Two debits on one account are checked together, not one at a time."""
    book = Ledger()
    book.issue("a", "consumer_available", 100)
    book.account("b", "consumer_available")
    book.account("c", "consumer_available")
    with pytest.raises(InsufficientFundsError):
        book.post(
            Journal("X", ("k", "net", 1), "adjustment",
                    postings=(Posting("a", 60), Posting("a", 60),
                              Posting("b", -100), Posting("c", -20)))
        )
    assert book.balance("a") == 100
    assert book.total_supply() == 100


def test_defect2_balances_always_reconcile_against_recorded_postings() -> None:
    """After any rejection, each account matches the sum of its postings."""
    book = _funded(5_000)
    book.settle_success("S1", "consumer-1", ["prov-A"], 2_000, SOLO_A, "J-1")
    with pytest.raises(InsufficientFundsError):
        book.settle_success("S2", "consumer-1", ["prov-B"], 9_999, SOLO_B, "J-2")
    _assert_reconciles(book)
    assert book.total_supply() == 5_000


# -- DEFECT 3: consume_reservation destroyed total supply ------------------


def test_defect3_consumption_conserves_total_supply() -> None:
    """Drawing on a hold moves credit to a payable; it does not destroy it."""
    book = Ledger()
    book.issue("c", "consumer_available", 1_000)
    book.reserve("r", "c", "consumer_available", 300)
    book.consume_reservation("r", 200)
    assert book.total_supply() == 1_000


def test_defect3_consumption_posts_an_auditable_journal() -> None:
    """The movement is recorded, so balances reconcile against the journal list."""
    book = Ledger()
    book.issue("c", "consumer_available", 1_000)
    book.reserve("r", "c", "consumer_available", 300)
    journals_before = len(book.journals())
    journal = book.consume_reservation("r", 200)
    assert journal.kind == "consumption"
    assert journal.total_delta() == 0
    assert {p.account: p.delta for p in journal.postings} == {
        "c/reserved": -200,
        "c/settlement_payable": 200,
    }
    assert len(book.journals()) == journals_before + 1
    assert book.balance("c/reserved") == 100
    assert book.balance("c/settlement_payable") == 200
    _assert_reconciles(book)


def test_defect3_consumption_is_idempotent_per_business_key() -> None:
    """The same (reservation, version) draws once; a higher version draws again."""
    book = Ledger()
    book.issue("c", "consumer_available", 1_000)
    book.reserve("r", "c", "consumer_available", 300)
    first = book.consume_reservation("r", 200, version=1)
    again = book.consume_reservation("r", 200, version=1)
    assert first.journal_id == again.journal_id
    assert book.balance("c/reserved") == 100
    assert book.balance("c/settlement_payable") == 200
    assert book.total_supply() == 1_000
    # A genuinely distinct draw needs a distinct version.
    book.consume_reservation("r", 100, version=2)
    assert book.balance("c/reserved") == 0
    assert book.balance("c/settlement_payable") == 300
    assert book.reservation_state("r") == "consumed"
    assert book.total_supply() == 1_000


def test_defect3_full_consumption_closes_the_hold_and_refuses_a_further_draw() -> None:
    """A hold drawn to zero is closed and rejects further draws."""
    book = Ledger()
    book.issue("c", "consumer_available", 1_000)
    book.reserve("r", "c", "consumer_available", 300)
    book.consume_reservation("r", 300)
    assert book.reservation_state("r") == "consumed"
    assert book.balance("c/settlement_payable") == 300
    # The closed hold is the guard; a draw against it never reaches the ledger.
    with pytest.raises(LedgerError):
        book.consume_reservation("r", 1, version=2)
    assert book.balance("c/settlement_payable") == 300
    assert book.total_supply() == 1_000


def test_defect3_overdrawing_a_hold_leaves_the_books_untouched() -> None:
    """A refused draw moves no credit and does not close the hold."""
    book = Ledger()
    book.issue("c", "consumer_available", 1_000)
    book.reserve("r", "c", "consumer_available", 300)
    with pytest.raises(InsufficientFundsError):
        book.consume_reservation("r", 301)
    assert book.balance("c/reserved") == 300
    assert book.balance("c/settlement_payable") == 0
    assert book.reservation_state("r") == "held"
    assert book.total_supply() == 1_000


def test_defect3_settlement_after_consumption_conserves_supply() -> None:
    """The payable is the platform's liability; settling the unit clears it."""
    book = Ledger()
    book.issue("c", "consumer_available", 1_000)
    book.reserve("r", "c", "consumer_available", 400)
    book.consume_reservation("r", 400)
    book.account("prov-A/pending", "provider_pending")
    # The hold moved 400 out of spendable, so the consumer settles from what
    # remains and the provider is paid from the payable.
    book.settle_success("S1", "c/settlement_payable", ["prov-A"], 400, SOLO_A, "J-1")
    assert book.balance("prov-A/pending") == 400
    assert book.balance("c/settlement_payable") == 0
    assert book.balance("c") == 600
    assert book.total_supply() == 1_000
    _assert_reconciles(book)


def test_defect3_rejects_non_positive_consumption() -> None:
    """A zero or negative draw is refused rather than silently ignored."""
    book = Ledger()
    book.issue("c", "consumer_available", 1_000)
    book.reserve("r", "c", "consumer_available", 300)
    for bad in (0, -100):
        with pytest.raises(LedgerError):
            book.consume_reservation("r", bad, version=1)
    assert book.balance("c/reserved") == 300
    assert book.total_supply() == 1_000


# -- DEFECT 4: refund / unfreeze / compensate_provider skipped idempotency --


def test_defect4_unfreeze_twice_moves_credit_once() -> None:
    """A repeated unfreeze returns the original journal and pays out once."""
    book = _funded()
    _pending_and_available(book, "prov-A")
    book.settle_success("S1", "consumer-1", ["prov-A"], 1_000, SOLO_A, "J-1")
    first = book.unfreeze("prov-A", "U-1", 400)
    second = book.unfreeze("prov-A", "U-2", 400)
    assert first.journal_id == second.journal_id
    assert book.balance("prov-A/available") == 400
    assert book.balance("prov-A/pending") == 600
    assert book.total_supply() == 10_000


def test_defect4_unfreeze_key_is_stable_across_calls() -> None:
    """The key no longer embeds a journal count that changes between calls."""
    book = _funded()
    _pending_and_available(book, "prov-A")
    book.settle_success("S1", "consumer-1", ["prov-A"], 1_000, SOLO_A, "J-1")
    first = book.unfreeze("prov-A", "U-1", 400, version=3)
    second = book.unfreeze("prov-A", "U-2", 400, version=3)
    assert first.idempotency_key == ("prov-A", "unfreeze", 3)
    assert first.idempotency_key == second.idempotency_key
    assert book.balance("prov-A/available") == 400


def test_defect4_unfreeze_at_a_new_version_is_a_distinct_payout() -> None:
    """Two genuine unfreezes use two versions and both pay out."""
    book = _funded()
    _pending_and_available(book, "prov-A")
    book.settle_success("S1", "consumer-1", ["prov-A"], 1_000, SOLO_A, "J-1")
    book.unfreeze("prov-A", "U-1", 400, version=1)
    book.unfreeze("prov-A", "U-2", 300, version=2)
    assert book.balance("prov-A/available") == 700
    assert book.balance("prov-A/pending") == 300
    assert book.total_supply() == 10_000


def test_defect4_refund_twice_does_not_corrupt_balances() -> None:
    """The second refund is a no-op rather than a mid-post failure."""
    book = _funded()
    journal, _ = book.settle_success("S1", "consumer-1", ["prov-A", "prov-B"], 1_000,
                                    CONTRACT, "J-1")
    first = book.refund(journal, "R-1")
    second = book.refund(journal, "R-2")
    assert first.journal_id == second.journal_id
    assert book.balance("consumer-1") == 10_000
    assert book.balance("prov-A/pending") == 0
    assert book.balance("prov-B/pending") == 0
    assert book.total_supply() == 10_000


def test_defect4_refund_at_a_new_version_is_rejected_not_double_credited() -> None:
    """Bumping the version cannot refund the same original twice."""
    book = _funded()
    journal, _ = book.settle_success("S1", "consumer-1", ["prov-A"], 1_000, SOLO_A, "J-1")
    book.refund(journal, "R-1", version=2)
    with pytest.raises(InsufficientFundsError):
        book.refund(journal, "R-2", version=3)
    assert book.balance("consumer-1") == 10_000
    assert book.balance("prov-A/pending") == 0
    assert book.total_supply() == 10_000


def test_defect4_compensate_provider_twice_moves_credit_once() -> None:
    """A repeated compensation returns the original journal and pays once."""
    book = _funded()
    book.fund_reserve(5_000)
    book.account("prov-A/pending", "provider_pending")
    first = book.compensate_provider("prov-A", 2_000, "C-1", "S1")
    second = book.compensate_provider("prov-A", 2_000, "C-2", "S1")
    assert first.journal_id == second.journal_id
    assert book.balance("prov-A/pending") == 2_000
    assert book.balance("platform_risk_fund/available") == 3_000
    assert book.total_supply() == 15_000


def test_defect4_refund_of_unposted_journal_is_still_refused() -> None:
    """Idempotency does not weaken the guard against refunding a fiction."""
    book = _funded()
    ghost = Journal("GHOST", ("g", "success", 1), "success",
                    postings=(Posting("consumer-1", -100), Posting("prov-A/pending", 100)))
    with pytest.raises(LedgerError):
        book.refund(ghost, "R-1")
    assert book.balance("consumer-1") == 10_000


def test_a_journal_id_cannot_be_reused_to_overwrite_history() -> None:
    """Reusing a journal ID for unrelated business is refused, not overwritten.

    Found while randomizing: re-posting a settlement under the same journal ID
    but a different business key silently replaced the recorded journal, so the
    ledger no longer explained its own balances.
    """
    book = _funded()
    book.settle_success("S1", "consumer-1", ["prov-A"], 1_000, SOLO_A, "J-1")
    original = book.journal("J-1")
    assert original is not None
    with pytest.raises(LedgerError) as excinfo:
        book.post(
            Journal("J-1", ("S2", "success", 1), "success",
                    postings=(Posting("consumer-1", -100), Posting("prov-A/pending", 100)))
        )
    assert "already records business key" in str(excinfo.value)
    # The original record survives untouched and the books still reconcile.
    assert book.journal("J-1") is original
    assert book.balance("prov-A/pending") == 1_000
    assert book.balance("consumer-1") == 9_000
    _assert_reconciles(book)


def test_reposting_the_identical_journal_is_still_idempotent() -> None:
    """The same journal under the same key returns the original, moving nothing."""
    book = _funded()
    book.settle_success("S1", "consumer-1", ["prov-A"], 1_000, SOLO_A, "J-1")
    journal = Journal("J-9", ("k", "replay", 1), "adjustment",
                      postings=(Posting("prov-A/pending", -100),
                                Posting("prov-A/available", 100)))
    book.account("prov-A/available", "provider_available")
    first = book.post(journal)
    before = book.balance("prov-A/available")
    second = book.post(journal)
    assert first is second
    assert book.balance("prov-A/available") == before
    _assert_reconciles(book)


# -- DEFECT 5: statement() reported pending earnings as spendable -----------


def test_defect5_statement_rejects_a_pending_sub_account() -> None:
    """Handing statement() a sub-account is refused, not reinterpreted."""
    book = _funded()
    book.settle_success("S1", "consumer-1", ["prov-A", "prov-B"], 1_000, CONTRACT, "J-1")
    with pytest.raises(LedgerError) as excinfo:
        book.statement("prov-A/pending")
    assert "sub-account" in str(excinfo.value)


def test_defect5_statement_never_reports_pending_as_available() -> None:
    """Pending earnings are reported as pending, never as spendable credit."""
    book = _funded()
    book.settle_success("S1", "consumer-1", ["prov-A", "prov-B"], 1_000, CONTRACT, "J-1")
    report = book.statement("prov-A")
    assert report["pending"] == 800
    assert report["available"] == 0
    assert report["total"] == 800
    # Every sub-account spelling is refused, not just the pending one.
    for suffix in ("/available", "/reserved", "/risk_reserved", "/pending",
                   "/settlement_payable"):
        with pytest.raises(LedgerError):
            book.statement(f"prov-A{suffix}")


def test_defect5_consumer_side_of_a_statement_is_still_reported() -> None:
    """Rejecting sub-accounts must not lose the consumer's own spendable credit."""
    book = _funded()
    book.issue("prov-A", "consumer_available", 5_000)
    report = book.statement("prov-A")
    assert report["available"] == 5_000
    assert report["pending"] == 0
    assert report["total"] == 5_000


def test_defect5_settlement_payable_is_visible_but_not_spendable() -> None:
    """A payable is reported in total, and is never offered as available."""
    book = Ledger()
    book.issue("c", "consumer_available", 1_000)
    book.reserve("r", "c", "consumer_available", 300)
    book.consume_reservation("r", 200)
    report = book.statement("c")
    assert report["settlement_payable"] == 200
    assert report["reserved"] == 100
    assert report["available"] == 700
    assert report["total"] == 1_000


# -- supply conservation under randomized operation sequences --------------


def _supply_invariant_cases(count: int = 500, seed: int = 20261006) -> None:
    """Drive randomized operation sequences and assert supply never leaks.

    `total_supply()` must equal the total explicitly issued plus the platform
    reserve funded, no matter how many reservations, settlements, refunds,
    unfreezes, compensations, and consumptions intervene — including the ones
    that raise partway through.
    """
    rng = random.Random(seed)
    for case in range(count):
        book = Ledger()
        issued = 0
        funded = 0
        contracts = [
            (["prov-A"], SOLO_A),
            (["prov-B"], SOLO_B),
            (["prov-A", "prov-B"], CONTRACT),
        ]
        _pending_and_available(book, "prov-A", "prov-B")

        holds: list[str] = []
        settled: list[Journal] = []
        for step in range(1, rng.randint(4, 18) + 1):
            action = rng.choice(
                ["issue", "fund", "reserve", "consume", "release", "settle",
                 "refund", "unfreeze", "compensate"]
            )
            consumer = f"consumer-{rng.randint(1, 3)}"
            amount = rng.randint(1, 900)
            try:
                if action == "issue":
                    book.issue(consumer, "consumer_available", amount)
                    issued += amount
                elif action == "fund":
                    book.fund_reserve(amount)
                    funded += amount
                elif action == "reserve":
                    hold = f"h-{case}-{step}"
                    book.reserve(hold, consumer, "consumer_available", amount)
                    holds.append(hold)
                elif action == "consume" and holds:
                    hold = rng.choice(holds)
                    book.consume_reservation(hold, amount, version=step)
                elif action == "release" and holds:
                    hold = holds.pop(0)
                    book.release(hold, f"JREL-{case}-{step}")
                elif action == "settle":
                    names, contract = rng.choice(contracts)
                    journal, _ = book.settle_success(
                        f"S-{case}-{step}", consumer, names, amount, contract,
                        f"JS-{case}-{step}",
                    )
                    settled.append(journal)
                elif action == "refund" and settled:
                    book.refund(rng.choice(settled), f"JRF-{case}-{step}")
                elif action == "unfreeze":
                    book.unfreeze(rng.choice(["prov-A", "prov-B"]),
                                  f"JU-{case}-{step}", amount, version=step)
                elif action == "compensate":
                    book.compensate_provider(rng.choice(["prov-A", "prov-B"]), amount,
                                             f"JC-{case}-{step}", f"SU-{case}-{step}")
            except (InsufficientFundsError, LedgerError):
                # A refused operation must leave the books exactly as they were.
                pass

            assert book.total_supply() == issued + funded, (
                f"case {case} step {step} ({action}) leaked supply: "
                f"{book.total_supply()} != {issued + funded}"
            )
            # A refused operation must not have left a partial posting behind.
            _assert_reconciles(book)


def test_supply_is_conserved_across_500_randomized_sequences() -> None:
    """total_supply changes only on issuance and reserve funding, over 500 cases."""
    _supply_invariant_cases(500)


def test_supply_conservation_holds_for_a_second_seed() -> None:
    """A different seed exercises different interleavings of the same operations."""
    _supply_invariant_cases(200, seed=987654321)


def test_repeated_randomized_operations_never_move_credit_twice() -> None:
    """Every operation replayed at its own key leaves balances untouched."""
    rng = random.Random(4242)
    for _ in range(120):
        book = _funded(20_000)
        book.fund_reserve(10_000)
        _pending_and_available(book, "prov-A", "prov-B")
        amount = rng.randint(100, 2_000)
        # Keep each operation affordable on its own so a replay is rejected by
        # the idempotency guard rather than by an exhausted balance. prov-A's
        # settlement is refunded and prov-B's is compensated and unfrozen, so
        # the two never compete for the same pending credit.
        solo = amount // 4 or 1
        journal, _ = book.settle_success("S1", "consumer-1", ["prov-A"], solo,
                                         SOLO_A, "J-1")
        recorded = SOLO_A.distribute(solo)
        compensation = amount // 8 or 1
        unfreeze_amount = compensation // 2 or 1
        # prov-A is settled then refunded, so it returns to zero; prov-B is
        # funded from the reserve and keeps its compensation.
        supply_before = book.total_supply()

        for _ in range(3):
            book.settle_success("S1", "consumer-1", ["prov-A"], solo, SOLO_A, "J-2",
                                already_recorded=recorded)
            book.refund(journal, "R-1", version=2)
            book.compensate_provider("prov-B", compensation, "C-1", "S1")
            book.unfreeze("prov-B", "U-1", unfreeze_amount, version=1)

        # Settling and refunding the same unit is a round trip to zero.
        assert book.balance("prov-A/pending") == 0
        assert book.balance("consumer-1") == 20_000
        # prov-B keeps exactly one compensation, less the one unfreeze payout.
        assert book.balance("prov-B/pending") == compensation - unfreeze_amount
        assert book.balance("prov-B/available") == unfreeze_amount
        assert book.total_supply() == supply_before
        _assert_reconciles(book)


# -- idempotency across every public money-moving operation ---------------


def test_every_public_money_operation_honors_its_business_key() -> None:
    """Each operation, replayed at the same key, moves credit exactly once."""
    book = Ledger()
    book.issue("consumer-1", "consumer_available", 50_000)
    for provider in ("prov-A", "prov-B"):
        book.account(f"{provider}/pending", "provider_pending")
        book.account(f"{provider}/available", "provider_available")
    book.fund_reserve(20_000)

    # settle_success
    book.settle_success("S1", "consumer-1", ["prov-A", "prov-B"], 5_000, CONTRACT, "J-1")
    settled = {
        "consumer-1": book.balance("consumer-1"),
        "prov-A/pending": book.balance("prov-A/pending"),
        "prov-B/pending": book.balance("prov-B/pending"),
    }
    for _ in range(3):
        book.settle_success("S1", "consumer-1", ["prov-A", "prov-B"], 5_000, CONTRACT, "J-1")
    assert book.balance("consumer-1") == settled["consumer-1"] == 45_000
    assert book.balance("prov-A/pending") == 4_000
    assert book.balance("prov-B/pending") == 1_000

    # reserve then consume_reservation
    book.reserve("r1", "consumer-1", "consumer_available", 10_000)
    assert book.balance("consumer-1") == 35_000
    assert book.balance("consumer-1/reserved") == 10_000
    with pytest.raises(LedgerError):
        book.reserve("r1", "consumer-1", "consumer_available", 10_000)
    assert book.balance("consumer-1/reserved") == 10_000

    book.consume_reservation("r1", 4_000, version=1)
    for _ in range(3):
        book.consume_reservation("r1", 4_000, version=1)
    assert book.balance("consumer-1/reserved") == 6_000
    assert book.balance("consumer-1/settlement_payable") == 4_000

    # release
    book.release("r1", "REL-1")
    for _ in range(3):
        with pytest.raises(LedgerError):
            book.release("r1", "REL-2")
    assert book.balance("consumer-1/reserved") == 0
    assert book.balance("consumer-1") == 41_000

    # compensate_provider (paid into prov-B, kept clear of prov-A's refund)
    book.compensate_provider("prov-B", 3_000, "C-1", "S9")
    for _ in range(3):
        book.compensate_provider("prov-B", 3_000, "C-2", "S9")
    assert book.balance("prov-B/pending") == 4_000
    assert book.balance("platform_risk_fund/available") == 17_000

    # unfreeze — prov-B only, so prov-A's settlement share stays available for
    # the refund below to claw back.
    book.unfreeze("prov-B", "U-1", 1_000, version=1)
    for _ in range(3):
        book.unfreeze("prov-B", "U-2", 1_000, version=1)
    assert book.balance("prov-B/available") == 1_000
    assert book.balance("prov-B/pending") == 3_000

    # refund — reverses J-1's postings, clawing back both beneficiaries' shares
    # and prov-B's unfrozen 1,000. A replay is a no-op, not a second reversal.
    original = book.journal("J-1")
    assert original is not None
    book.refund(original, "R-1")
    for _ in range(3):
        book.refund(original, "R-2")
    assert book.balance("prov-A/pending") == 0
    # prov-B: 3_000 compensation + 1_000 settlement share - 1_000 unfrozen,
    # then the refund claws the settlement share back, leaving 2_000.
    assert book.balance("prov-B/pending") == 2_000
    # The refund reverses J-1 only, so the unfreeze payout is untouched: a
    # refund claws back the settlement share, not credit already released.
    assert book.balance("prov-B/available") == 1_000
    # Consumer: 50_000 issued, -5_000 settled, +5_000 refunded, and -4_000 still
    # sitting in the payable that consumed from the hold.
    assert book.balance("consumer-1") == 46_000
    assert book.balance("consumer-1/settlement_payable") == 4_000

    # Explicit issuance is the one operation that mints, and it is counted.
    assert book.total_supply() == 50_000 + 20_000
    _assert_reconciles(book)


def test_issue_is_the_only_creator_of_supply() -> None:
    """fund_reserve aside, nothing outside issuance mints a milli-credit."""
    book = Ledger()
    book.issue("consumer-1", "consumer_available", 10_000)
    _pending_and_available(book, "prov-A", "prov-B")
    book.fund_reserve(5_000)
    assert book.total_supply() == 15_000
    book.reserve("r", "consumer-1", "consumer_available", 4_000)
    assert book.total_supply() == 15_000
    book.consume_reservation("r", 4_000)
    assert book.total_supply() == 15_000
    book.settle_success("S1", "consumer-1", ["prov-A"], 4_000, SOLO_A, "J-1")
    assert book.total_supply() == 15_000
    journal = book.journal("J-1")
    assert journal is not None
    book.refund(journal, "R-1")
    assert book.total_supply() == 15_000
    book.compensate_provider("prov-B", 1_000, "C-1", "S1")
    book.unfreeze("prov-B", "U-1", 1_000, version=1)
    assert book.total_supply() == 15_000
    _assert_reconciles(book)


# -- net_adjustment's previous_total parameter -----------------------------


def test_net_adjustment_rejects_a_previous_total_that_contradicts_the_record() -> None:
    """previous_total is a real guard, not an ignored argument."""
    with pytest.raises(AllocationError) as excinfo:
        net_adjustment(9_999, 10_000, SOLO_A, {"prov-A": 8_700})
    assert "disagrees" in str(excinfo.value)


def test_net_adjustment_documented_invariant_holds_exactly() -> None:
    """Deltas sum to new_total minus what was already recorded."""
    recorded = CONTRACT.distribute(1_000)
    deltas = net_adjustment(1_000, 1_500, CONTRACT, recorded)
    assert sum(deltas.values()) == 1_500 - 1_000
    assert deltas == {"prov-A": 400, "prov-B": 100}


def test_net_adjustment_rejects_a_negative_previous_total() -> None:
    """A negative prior total is refused before any arithmetic runs."""
    with pytest.raises(AllocationError):
        net_adjustment(-1, 100, SOLO_A, {})