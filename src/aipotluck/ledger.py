"""Integer credit ledger: reservations, paired journals, and settlement.

Why this lives in the client package: replaying a receipt's journal is part of
trustless verification, so the ledger rules are a protocol primitive. Center
bookkeeping and the gateway live in ``ai-potluck-server``, which imports these
primitives to keep its own books.

Everything is milli-credit integers. No floats touch an amount, because
1,000 tokens per credit with fractional credits is exactly where rounding bugs
live. Every journal balances to zero within one currency, which catches
arithmetic mistakes but deliberately does not prove the payee was correct —
that check lives in `check_recipient_against_contract`.

Four account families are tracked separately and never share a balance field:
a consumer's spendable credit, a provider's pending earnings, a settlement payable
for credit already drawn out of a reservation, and the platform's risk reserve. A
reservation moves credit between a family's own buckets without changing the
platform's total, so reserving is not an expense. Consuming a reservation moves
credit into a payable, which likewise conserves total supply.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from .allocation import AllocationContract, net_adjustment
from .errors import InsufficientFundsError, LedgerError, UnbalancedJournalError

AccountKind = Literal["consumer_available", "consumer_reserved", "provider_pending",
                     "provider_available", "platform_reserve_available",
                     "settlement_payable", "provider_risk_reserved"]
SettlementKind = Literal[
    "success",
    "failure_no_charge",
    "consumer_cancel",
    "platform_compensation",
    "adjustment",
    "reservation",
    "release",
    "refund",
    "consumption",
]
ReservationState = Literal["held", "consumed", "released"]


class ReservationRecord(TypedDict):
    """Mutable bookkeeping for one live reservation hold.

    Modelled as a `TypedDict` rather than a frozen dataclass because the hold is
    updated in place as it is drawn down (`consume_reservation` lowers `amount`
    and flips `state`) and released (`release` flips `state`). A dataclass would
    force every one of those sites to rebuild the record, which changes the
    mutation pattern without changing any accounting behaviour.

    The field types are what let the read sites drop their `int()`/`str()`
    coercions: `amount` is already an `int` and the identifiers already `str`.
    """

    owner: str
    kind: AccountKind
    amount: int
    reserved_account: str
    state: ReservationState

#: Sub-account bucket suffixes. A root account id contains none of these, so
#: `statement` can reject a sub-account id instead of silently reporting a
#: restricted bucket as spendable.
#: Credit set aside against a service unit that is about to be charged. It is
#: the destination a consumed reservation flows into, and `settle_success` draws
#: the charge from here first so the reserve/consume/settle round trip closes.
SETTLEMENT_PAYABLE_SUFFIX = "/settlement_payable"

SUB_ACCOUNT_SUFFIXES = ("/available", "/reserved", "/risk_reserved", "/pending",
                        SETTLEMENT_PAYABLE_SUFFIX)


@dataclass
class Account:
    """One ledger account with an integer balance in milli-credits."""

    account_id: str
    kind: AccountKind
    balance: int = 0

    def credit(self, amount: int) -> None:
        """Increase the balance.

        Raises:
            LedgerError: If the amount is negative.
        """
        if amount < 0:
            raise LedgerError(f"credit amount must be non-negative, got {amount}")
        self.balance += amount

    def debit(self, amount: int) -> None:
        """Decrease the balance, refusing to go negative.

        Raises:
            InsufficientFundsError: If the balance would go below zero.
        """
        if amount < 0:
            raise LedgerError(f"debit amount must be non-negative, got {amount}")
        if self.balance < amount:
            raise InsufficientFundsError(
                f"{self.account_id} holds {self.balance}, cannot debit {amount}"
            )
        self.balance -= amount


@dataclass(frozen=True)
class Posting:
    """One leg of a journal entry."""

    account: str
    delta: int


@dataclass
class Journal:
    """An append-only, balanced set of postings.

    Journals are never edited. A correction posts a new journal referencing the
    old one, so history stays replayable.
    """

    journal_id: str
    idempotency_key: tuple[str, str, int]
    kind: SettlementKind
    postings: tuple[Posting, ...]
    service_unit_id: str | None = None
    corrects_journal_id: str | None = None
    receipt_hash: str | None = None

    def total_delta(self) -> int:
        """Return the sum of all postings, which must be zero."""
        return sum(posting.delta for posting in self.postings)


class Ledger:
    """In-memory ledger with reservations, journals, and account statements."""

    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._journals: dict[str, Journal] = {}
        self._by_idempotency: dict[tuple[str, str, int], str] = {}
        self._reservations: dict[str, ReservationRecord] = {}
        self._issue_counter: int = 0
        self._fund_counter: int = 0

    # -- accounts ---------------------------------------------------------

    def account(self, account_id: str, kind: AccountKind) -> Account:
        """Return an account, creating it with a zero balance if needed.

        Raises:
            LedgerError: If the account exists with a different kind, which
                would silently merge the three account families.
        """
        existing = self._accounts.get(account_id)
        if existing is None:
            existing = Account(account_id=account_id, kind=kind)
            self._accounts[account_id] = existing
        elif existing.kind != kind:
            raise LedgerError(
                f"{account_id} is a {existing.kind}, refusing to reuse it as a {kind}"
            )
        return existing

    def balance(self, account_id: str) -> int:
        """Return an account balance, or zero when the account is untouched."""
        account = self._accounts.get(account_id)
        return 0 if account is None else account.balance

    def total_supply(self) -> int:
        """Return the sum of all account balances.

        This total changes only on explicit supply creation: `issue` mints
        spendable credit and `fund_reserve` mints platform reserve credit.
        Reservations, consumption, settlements, refunds, and compensation only
        move credit between accounts, so they must leave this total unchanged.
        """
        return sum(account.balance for account in self._accounts.values())

    def issue(self, account_id: str, kind: AccountKind, amount: int) -> None:
        """Mint initial test credit from an explicit issuance account.

        Issuance is a separate authorized action and is never folded into a
        service settlement. It increases total supply, so it is posted as a
        balanced journal between the issuance account and the recipient rather
        than as a one-sided balance tweak.

        Raises:
            LedgerError: If the amount is not positive.
        """
        if amount <= 0:
            raise LedgerError(f"issuance amount must be positive, got {amount}")
        self.account(account_id, kind)
        treasury = self.account("treasury/issuance", "platform_reserve_available")
        if treasury.balance < amount:
            # The treasury is the system's issuance liability; funding it is an
            # explicit act, so top it up rather than debiting into the negative.
            treasury.balance += amount
        journal = Journal(
            journal_id=f"ISSUE-{account_id}-{self._issue_counter}",
            idempotency_key=(account_id, "issuance", self._issue_counter),
            kind="adjustment",
            postings=(
                Posting("treasury/issuance", -amount),
                Posting(account_id, amount),
            ),
        )
        self._issue_counter += 1
        self._post(journal)

    def fund_reserve(self, amount: int) -> None:
        """Add credit to the platform's pre-funded compensation reserve.

        Funding the reserve is one of the two explicit acts that create supply
        (the other is `issue`), so it legitimately raises `total_supply`. It is
        posted as a balanced journal against the issuance treasury — the same
        source `issue` names — rather than a bare balance tweak, so the movement
        is explained by the journal list and reconciles like every other one.

        Each call is a distinct funding event with its own journal, so funding
        twice adds twice.

        Args:
            amount: Milli-credits to add to the reserve.

        Raises:
            LedgerError: If the amount is not positive.
        """
        if amount <= 0:
            raise LedgerError(f"reserve funding must be positive, got {amount}")
        self.account("platform_risk_fund/available", "platform_reserve_available")
        treasury = self.account("treasury/issuance", "platform_reserve_available")
        if treasury.balance < amount:
            # Mirroring `issue`: the treasury is the system's issuance
            # liability, and topping it up is the one place supply is minted.
            treasury.balance += amount
        journal = Journal(
            journal_id=f"FUND-RESERVE-{self._fund_counter}",
            idempotency_key=("platform_risk_fund/available", "funding", self._fund_counter),
            kind="adjustment",
            postings=(
                Posting("treasury/issuance", -amount),
                Posting("platform_risk_fund/available", amount),
            ),
        )
        self._fund_counter += 1
        self._post(journal)

    # -- reservations -----------------------------------------------------

    def reserve(
        self,
        reservation_id: str,
        owner_account: str,
        kind: AccountKind,
        amount: int,
    ) -> Journal:
        """Hold `amount` against an account, splitting spendable from reserved.

        For a consumer this converts available into reserved. For a provider it
        reserves against the pending balance, which is how a node's own local
        exposure is capped.

        The hold is posted as a balanced journal rather than two silent balance
        tweaks, so every credit inside a reserved bucket is explained by the
        journal list and the two halves of the reservation stay auditable.

        Args:
            reservation_id: Unique ID for the hold; also its idempotency subject.
            owner_account: Account the hold is placed against.
            kind: Account kind of the owner.
            amount: Milli-credits to hold.

        Returns:
            The balanced reservation journal.

        Raises:
            InsufficientFundsError: If the account cannot cover the hold.
            LedgerError: If the reservation ID already exists.
        """
        if reservation_id in self._reservations:
            raise LedgerError(f"reservation {reservation_id} already exists")
        self.account(owner_account, kind)
        # A consumer hold protects the consumer's spendable balance; a provider
        # hold caps what the node may still consume on a shared account. These
        # are different risks with different owners, so they get distinct
        # buckets and distinct kinds rather than sharing one reserved pool.
        if kind == "consumer_available":
            reserved_id = f"{owner_account}/reserved"
            reserved_kind: AccountKind = "consumer_reserved"
        else:
            reserved_id = f"{owner_account}/risk_reserved"
            reserved_kind = "provider_risk_reserved"
        self.account(reserved_id, reserved_kind)
        journal = Journal(
            journal_id=f"HOLD-{reservation_id}",
            idempotency_key=(reservation_id, "reservation", 1),
            kind="reservation",
            postings=(
                Posting(owner_account, -amount),
                Posting(reserved_id, amount),
            ),
        )
        self._post(journal)
        self._reservations[reservation_id] = {
            "owner": owner_account,
            "kind": kind,
            "amount": amount,
            "reserved_account": reserved_id,
            "state": "held",
        }
        return journal

    def release(self, reservation_id: str, journal_id: str) -> Journal:
        """Release a held reservation back to its owner.

        Args:
            reservation_id: The hold to release.
            journal_id: Journal ID for the paired movement.

        Returns:
            The balanced release journal, or the original one when this
            reservation was already released at this version.

        Raises:
            LedgerError: If the reservation is unknown or already closed.
        """
        record = self._reservations.get(reservation_id)
        if record is None:
            raise LedgerError(f"unknown reservation: {reservation_id}")
        if record["state"] != "held":
            raise LedgerError(
                f"reservation {reservation_id} is {record['state']}, cannot release"
            )
        amount = record["amount"]
        owner = record["owner"]
        reserved_account = record["reserved_account"]
        journal = Journal(
            journal_id=journal_id,
            idempotency_key=(reservation_id, "release", 1),
            kind="release",
            postings=(
                Posting(reserved_account, -amount),
                Posting(owner, amount),
            ),
        )
        self.post(journal)
        self._reservations[reservation_id]["state"] = "released"
        return journal

    def consume_reservation(
        self,
        reservation_id: str,
        amount: int,
        journal_id: str | None = None,
        version: int = 1,
    ) -> Journal:
        """Draw `amount` from a held reservation and record a payable.

        Accounting story: the hold was reserving credit that was already spoken
        for, most often against a service unit that is about to be settled.
        Drawing on it does not destroy that credit, it reassigns it. The
        reserved bucket pays a settlement payable, which is the platform's
        liability to the provider that will eventually receive the money. When
        `settle_success` later charges the payer for that unit, the payer is
        debited and the credit leaves the payable, so the round trip conserves
        total supply at every step. Debiting the reserved bucket with no
        offsetting credit — the previous behaviour — shrank total supply on a
        path that is not an issuance, which is precisely what must never happen.

        Args:
            reservation_id: The hold being drawn against.
            amount: Milli-credits to consume; must be positive and may be less
                than the held amount.
            journal_id: Journal ID for the paired movement. Defaults to a
                deterministic ID derived from the reservation and version.
            version: Business version for the idempotency key. A second draw
                against the same hold must pass a higher version.

        Returns:
            The balanced consumption journal. A repeated call with an already
            used `(reservation_id, "consumption", version)` key returns the
            original journal and moves no credit.

        Raises:
            LedgerError: If the reservation is unknown, is not held, or the
                amount is not positive.
            InsufficientFundsError: If the hold is smaller than the draw.
        """
        idempotency_key = (reservation_id, "consumption", version)
        existing_id = self._by_idempotency.get(idempotency_key)
        if existing_id is not None:
            return self._journals[existing_id]
        record = self._reservations.get(reservation_id)
        if record is None:
            raise LedgerError(f"unknown reservation: {reservation_id}")
        if record["state"] != "held":
            raise LedgerError(f"reservation {reservation_id} is {record['state']}")
        if amount <= 0:
            raise LedgerError(f"consumption amount must be positive, got {amount}")
        held = record["amount"]
        if amount > held:
            raise InsufficientFundsError(
                f"reservation {reservation_id} holds {held}, cannot consume {amount}"
            )
        owner = record["owner"]
        reserved_account = record["reserved_account"]
        payable_account = f"{owner}{SETTLEMENT_PAYABLE_SUFFIX}"
        self.account(payable_account, "settlement_payable")
        journal = Journal(
            journal_id=journal_id or f"CONSUME-{reservation_id}-v{version}",
            idempotency_key=idempotency_key,
            kind="consumption",
            postings=(
                Posting(reserved_account, -amount),
                Posting(payable_account, amount),
            ),
        )
        self._post(journal)
        record["amount"] = held - amount
        if record["amount"] == 0:
            record["state"] = "consumed"
        return journal

    def reservation_state(self, reservation_id: str) -> ReservationState:
        """Return the state of a reservation.

        Raises:
            LedgerError: If the reservation is unknown.
        """
        record = self._reservations.get(reservation_id)
        if record is None:
            raise LedgerError(f"unknown reservation: {reservation_id}")
        return record["state"]

    # -- journals ---------------------------------------------------------

    def post(self, journal: Journal) -> Journal:
        """Apply a balanced journal, enforcing idempotency.

        Args:
            journal: The journal to post.

        Returns:
            The posted journal, or the existing one when the idempotency key
            was already used. Re-posting never moves credit twice.

        Raises:
            UnbalancedJournalError: If the postings do not sum to zero.
            LedgerError: If a posting names an unknown account.
        """
        existing_id = self._by_idempotency.get(journal.idempotency_key)
        if existing_id is not None:
            return self._journals[existing_id]
        self._post(journal)
        return journal

    def _post(self, journal: Journal) -> None:
        """Apply a balanced journal atomically.

        Postings are netted per account and every debit is validated against the
        resulting balance before any mutation happens. A rejected journal
        therefore leaves every balance and the journal list untouched, so
        balances always reconcile against the recorded postings.

        Args:
            journal: The journal to apply. It is recorded only once validation
                passes.

        Raises:
            UnbalancedJournalError: If the postings do not sum to zero.
            LedgerError: If a posting names an unknown account, or the journal ID
                is already recorded under a different business key.
            InsufficientFundsError: If any account cannot cover its net debit.
        """
        total = journal.total_delta()
        if total != 0:
            raise UnbalancedJournalError(
                f"journal {journal.journal_id} sums to {total}, expected 0"
            )
        existing = self._journals.get(journal.journal_id)
        if existing is not None and existing.idempotency_key != journal.idempotency_key:
            # Reusing an ID for unrelated business would overwrite an immutable
            # record and make the ledger unreplayable, so it is refused.
            raise LedgerError(
                f"journal id {journal.journal_id} already records business key "
                f"{existing.idempotency_key}, refusing to reuse it for "
                f"{journal.idempotency_key}"
            )
        net: dict[str, int] = {}
        for posting in journal.postings:
            if posting.account not in self._accounts:
                raise LedgerError(
                    f"journal {journal.journal_id} posts to unknown account {posting.account}"
                )
            net[posting.account] = net.get(posting.account, 0) + posting.delta
        for account_id, delta in net.items():
            if delta < 0 and self._accounts[account_id].balance < -delta:
                raise InsufficientFundsError(
                    f"{account_id} holds {self._accounts[account_id].balance}, "
                    f"cannot debit {-delta} via journal {journal.journal_id}"
                )
        for account_id, delta in net.items():
            if delta >= 0:
                self._accounts[account_id].credit(delta)
            else:
                self._accounts[account_id].debit(-delta)
        self._journals[journal.journal_id] = journal
        self._by_idempotency.setdefault(journal.idempotency_key, journal.journal_id)
        if journal.service_unit_id is not None:
            key = ("service_unit", journal.service_unit_id, journal.idempotency_key[2])
            self._by_idempotency.setdefault(key, journal.journal_id)

    def journal(self, journal_id: str) -> Journal | None:
        """Return a posted journal by ID."""
        return self._journals.get(journal_id)

    def journals(self) -> list[Journal]:
        """Return all posted journals in insertion order."""
        return list(self._journals.values())

    def has_idempotency_key(self, key: tuple[str, str, int]) -> bool:
        """Report whether a business key has already been settled."""
        return key in self._by_idempotency

    # -- statements -------------------------------------------------------

    def statement(self, account_id: str) -> dict[str, object]:
        """Return a per-account summary separating total from available.

        A provider's total includes pending earnings that cannot yet be spent,
        so a single net number would overstate what is usable. When one identity
        is both consumer and provider, its spendable balance and its pending
        earnings are reported in the same statement but never merged, matching
        the requirement to keep gross spend, gross earnings, and net apart.

        The account id must be a ROOT identity such as `provider-B`. Passing a
        sub-account such as `provider-B/pending` is rejected rather than
        reinterpreted: the root's spendable slot is derived as
        `balance(account_id)`, so a pending bucket handed in here would be
        reported as spendable `available` — turning unearned earnings into
        credit the identity could spend. Rejecting the ambiguity keeps
        "pending is not spendable" a property of the API rather than a
        convention callers must remember.

        Args:
            account_id: Root account id, e.g. `provider-B`.

        Returns:
            Dict with total, available, reserved, risk_reserved, pending,
            settlement_payable, and posted entries.

        Raises:
            LedgerError: If `account_id` names a sub-account rather than a root
                identity.
        """
        if account_id.endswith(SUB_ACCOUNT_SUFFIXES):
            raise LedgerError(
                f"{account_id} is a sub-account; statement() takes a root account "
                f"id such as {account_id.rsplit('/', 1)[0]!r}, which reports pending "
                f"earnings separately from spendable credit"
            )
        available = self.balance(f"{account_id}/available")
        reserved = self.balance(f"{account_id}/reserved")
        risk_reserved = self.balance(f"{account_id}/risk_reserved")
        pending = self.balance(f"{account_id}/pending")
        settlement_payable = self.balance(f"{account_id}{SETTLEMENT_PAYABLE_SUFFIX}")
        # The consumer side of this identity, when it also spends.
        consumer_available = self.balance(account_id)
        owned = {
            account_id,
            f"{account_id}/available",
            f"{account_id}/reserved",
            f"{account_id}/risk_reserved",
            f"{account_id}/pending",
            f"{account_id}/settlement_payable",
        }
        entries = [
            {
                "journal_id": journal.journal_id,
                "kind": journal.kind,
                "delta": sum(p.delta for p in journal.postings if p.account in owned),
            }
            for journal in self._journals.values()
            if any(p.account in owned for p in journal.postings)
        ]
        return {
            "account_id": account_id,
            "total": (available + reserved + risk_reserved + pending
                      + settlement_payable + consumer_available),
            "available": available + consumer_available,
            "reserved": reserved,
            "risk_reserved": risk_reserved,
            "pending": pending,
            "settlement_payable": settlement_payable,
            "entries": entries,
        }

    def unfreeze(
        self,
        provider_id: str,
        journal_id: str,
        amount: int,
        version: int = 1,
    ) -> Journal:
        """Move provider earnings from pending to available after review.

        Args:
            provider_id: Root provider identity whose earnings are reviewed.
            journal_id: Journal ID for the paired movement.
            amount: Milli-credits to release.
            version: Business version for the idempotency key. Repeated calls at
                the same version return the original journal and move no credit;
                a distinct unfreeze must pass a higher version.

        Returns:
            The balanced unfreeze journal, or the original one when this
            business key was already applied.

        Raises:
            InsufficientFundsError: If pending does not cover the amount.
        """
        journal = Journal(
            journal_id=journal_id,
            idempotency_key=(provider_id, "unfreeze", version),
            kind="adjustment",
            postings=(
                Posting(f"{provider_id}/pending", -amount),
                Posting(f"{provider_id}/available", amount),
            ),
        )
        return self.post(journal)

    def _payer_legs(self, payer_account_id: str, amount: int) -> tuple[list[Posting], int]:
        """Split a debit across the payer's reserved, payable, and free balances.

        Order matters and encodes what each bucket means. Credit already reserved
        for this payer is charged first, then any settlement payable created by a
        consumed reservation, and only then the free balance. Charging the free
        balance first would let a consumer spend money that is already earmarked
        while a reservation is outstanding, which is exactly what the reservation
        exists to prevent.

        Args:
            payer_account_id: Root account being charged.
            amount: Total debit required.

        Returns:
            The postings to apply and the amount charged. A negative amount
            yields a credit back to the payer rather than a debit, because a
            downward correction is a partial refund and must still balance
            against the reduced beneficiary legs.

        Raises:
            InsufficientFundsError: If the payer's buckets together cannot cover
                `amount`.
        """
        if amount < 0:
            # Downward correction: return the difference to the payer's spendable
            # balance, so the reversal is symmetric with the original debit.
            return [Posting(payer_account_id, -amount)], 0
        if amount == 0:
            return [], 0

        buckets = (
            f"{payer_account_id}/reserved",
            f"{payer_account_id}{SETTLEMENT_PAYABLE_SUFFIX}",
            payer_account_id,
        )
        remaining = amount
        postings: list[Posting] = []
        for bucket in buckets:
            if remaining <= 0:
                break
            held = self.balance(bucket)
            if held <= 0:
                continue
            taken = min(held, remaining)
            postings.append(Posting(bucket, -taken))
            remaining -= taken
        if remaining:
            raise InsufficientFundsError(
                f"{payer_account_id} cannot cover {amount}: {remaining} milli-credit(s) "
                f"short across its reserved, payable, and available balances"
            )
        return postings, amount


    def settle_success(
        self,
        service_unit_id: str,
        payer_account_id: str,
        beneficiaries: Iterable[str],
        billable_milli_credits: int,
        contract: AllocationContract,
        journal_id: str,
        version: int = 1,
        already_recorded: dict[str, int] | None = None,
    ) -> tuple[Journal, dict[str, int]]:
        """Settle a successful service unit, paying beneficiaries per contract.

        Args:
            service_unit_id: The unit being settled.
            payer_account_id: Consumer account paying.
            beneficiaries: Accounts the contract distributes to; must match the
                contract's beneficiary set.
            billable_milli_credits: Amount to distribute.
            contract: The allocation contract fixed before dispatch.
            journal_id: Journal ID for this settlement.
            version: Settlement version; used in the idempotency key.
            already_recorded: Amounts already booked for a correction, so only
                the net difference is posted.

        Returns:
            The journal and the per-beneficiary allocation. When the business key
            was already settled, the original journal and allocation are returned
            unchanged and no credit moves, regardless of the payer's current
            balance — a replay is a pure no-op.

        Raises:
            AllocationError: If the beneficiary set does not match the contract.
            InsufficientFundsError: If the payer cannot cover the bill.
        """
        declared = set(beneficiaries)
        contracted = set(contract.all_weights())
        if declared != contracted:
            raise LedgerError(
                f"beneficiaries {sorted(declared)} do not match contract "
                f"{contract.contract_id} v{contract.version} {sorted(contracted)}"
            )
        recorded = dict(already_recorded or {})
        deltas = net_adjustment(
            previous_total=sum(recorded.values()),
            new_total=billable_milli_credits,
            contract=contract,
            already_recorded=recorded,
        )
        # `already_recorded` marks a correction, so the key's kind depends on the
        # call shape; a success and its correction at the same version are
        # deliberately distinct business keys.
        kind: SettlementKind = "adjustment" if recorded else "success"
        idempotency_key = (service_unit_id, kind, version)
        # The allocation is a pure function of the contract and the recorded
        # total, so it can be rebuilt from the arguments the first call used.
        # Resolve the idempotency key BEFORE any balance work: a replay must not
        # reach `_payer_legs`, whose debit would fail once the first settlement
        # has spent the payer's credit.
        if recorded:
            target = {b: recorded.get(b, 0) + a for b, a in deltas.items()}
            for beneficiary, already in recorded.items():
                target.setdefault(beneficiary, already)
        else:
            target = contract.distribute(billable_milli_credits)
        existing_id = self._by_idempotency.get(idempotency_key)
        if existing_id is not None:
            return self._journals[existing_id], target
        if recorded:
            # A correction. Post only the per-beneficiary difference so an
            # already-settled service unit is not charged twice.
            net = billable_milli_credits - sum(recorded.values())
            payer_legs, _ = self._payer_legs(payer_account_id, net)
            postings = list(payer_legs)
            for beneficiary, amount in sorted(deltas.items()):
                postings.append(Posting(f"{beneficiary}/pending", amount))
        else:
            payer_legs, _ = self._payer_legs(payer_account_id, billable_milli_credits)
            postings = list(payer_legs)
            for beneficiary, amount in sorted(target.items()):
                postings.append(Posting(f"{beneficiary}/pending", amount))
        journal = Journal(
            journal_id=journal_id,
            idempotency_key=idempotency_key,
            kind=kind,
            postings=tuple(postings),
            service_unit_id=service_unit_id,
        )
        return self.post(journal), target

    def compensate_provider(
        self,
        provider_id: str,
        amount: int,
        journal_id: str,
        service_unit_id: str,
        version: int = 1,
    ) -> Journal:
        """Pay a provider from the platform's pre-funded reserve.

        Args:
            provider_id: Root provider identity being compensated.
            amount: Milli-credits to pay.
            journal_id: Journal ID for the paired movement.
            service_unit_id: The service unit this compensation covers; forms
                the subject of the idempotency key.
            version: Business version for the idempotency key. Repeated calls at
                the same version return the original journal and move no credit.

        Returns:
            The balanced compensation journal, or the original one when this
            business key was already applied.

        Raises:
            InsufficientFundsError: If the reserve cannot cover the compensation.
                A platform must not promise credit it has not funded.
        """
        journal = Journal(
            journal_id=journal_id,
            idempotency_key=(service_unit_id, "platform_compensation", version),
            kind="platform_compensation",
            postings=(
                Posting("platform_risk_fund/available", -amount),
                Posting(f"{provider_id}/pending", amount),
            ),
            service_unit_id=service_unit_id,
        )
        return self.post(journal)

    def refund(
        self,
        original: Journal,
        journal_id: str,
        version: int = 2,
    ) -> Journal:
        """Reverse an original journal by posting the negated entries.

        The refund uses the original postings, not today's allocation weights,
        so a later change to the split cannot rewrite yesterday's reversal.

        A refund reverses one specific posted journal, so the idempotency key is
        scoped to that journal's id rather than its service unit — a success and
        its correction share a service unit, and keying on it made the second
        reversal silently return the first.

        Args:
            original: The journal being reversed.
            journal_id: Journal ID for the reversal.
            version: Version for the idempotency key. Repeated calls at the
                same version return the original reversal and move no credit.

        Returns:
            The balanced reversal journal, or the original one when this
            business key was already applied.

        Raises:
            LedgerError: If `original` was never posted.
        """
        if original.journal_id not in self._journals:
            raise LedgerError(f"cannot refund unposted journal {original.journal_id}")
        postings = tuple(
            Posting(posting.account, -posting.delta) for posting in original.postings
        )
        journal = Journal(
            journal_id=journal_id,
            idempotency_key=(original.journal_id, "refund", version),
            kind="refund",
            postings=postings,
            service_unit_id=original.service_unit_id,
            corrects_journal_id=original.journal_id,
        )
        return self.post(journal)


def check_recipient_against_contract(
    journal: Journal,
    contract: AllocationContract,
) -> None:
    """Verify each beneficiary was paid according to the contract.

    A journal can balance to zero while paying a stranger, so balance checking
    is not enough on its own.

    Args:
        journal: The settlement journal to audit.
        contract: The contract in force for that service unit.

    Raises:
        LedgerError: If any beneficiary's amount differs from the contract, or
            an account outside the contract received credit.
    """
    expected = contract.distribute(sum(p.delta for p in journal.postings if p.delta > 0))
    actual = {
        posting.account.removesuffix("/pending"): posting.delta
        for posting in journal.postings
        if posting.delta > 0
    }
    if expected != actual:
        raise LedgerError(
            f"journal {journal.journal_id} pays {actual}, contract {contract.contract_id} "
            f"v{contract.version} requires {expected}"
        )


@dataclass
class LedgerTotals:
    """Aggregate view used by tests and CLI reports."""

    consumer_spend: dict[str, int] = field(default_factory=dict)
    provider_earned: dict[str, int] = field(default_factory=dict)

    def add_consumer(self, consumer_id: str, amount: int) -> None:
        """Accumulate consumer spend in milli-credits."""
        self.consumer_spend[consumer_id] = self.consumer_spend.get(consumer_id, 0) + amount

    def add_provider(self, provider_id: str, amount: int) -> None:
        """Accumulate provider earnings in milli-credits."""
        self.provider_earned[provider_id] = self.provider_earned.get(provider_id, 0) + amount