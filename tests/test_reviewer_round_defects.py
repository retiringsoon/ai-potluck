"""Regressions for the defects the reviewer found in the round after the last one.

The reviewer's sharpest point was that the existing retry tests observed only the
factory's side effects and never asserted the resulting branch state, which is
exactly why a livelock slipped through. These tests assert the branch state.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from conftest import SETTLEMENT_AT, Keyring, contract_hash, signed_authorization

from aipotluck.allocation import AllocationContract
from aipotluck.coordinator import Coordinator
from aipotluck.errors import IdentityBindingError, SettlementError
from aipotluck.identity import IdentityRegistry
from aipotluck.ledger import Ledger
from aipotluck.metering import Measurement, NormalizedUsage
from aipotluck.records import SCHEMA_AUTHORIZATION, SCHEMA_USAGE, SignedRecord
from aipotluck.settlement import SettlementEngine
from aipotluck.statemachine import RecoveryContext
from aipotluck.transparency import MerkleLog

CONTRACT = AllocationContract(contract_id="c", version=1, weights={"provider-B": 100})


def _context() -> RecoveryContext:
    return RecoveryContext(
        attempt_status="ACCEPTED",
        execution_certainty="not_submitted",
        output_committed=False,
        buffered_mode=False,
        original_execution_resumable=False,
        input_replayable=True,
        deadline_remaining_ms=30_000,
        has_external_side_effects=False,
        duplicate_execution_policy="bounded_model_only",
    )


def _retry(
    coordinator: Coordinator, request_id: str, from_attempt: str, new_id: str
) -> object:
    return coordinator.handle_attempt_failure(
        request_id,
        from_attempt,
        _context(),
        new_attempt_factory=lambda epoch: coordinator.open_attempt(
            request_id, new_id, f"res-{new_id}", "provider-B"
        ),
    )


def test_retry_leaves_exactly_one_publishable_branch() -> None:
    """A retry must not supersede the very branch it installs.

    The natural factory is `open_attempt`, which registers the replacement. An
    earlier version then ran `failover`, which saw the replacement as the current
    branch and marked it superseded by itself, leaving the request with no
    output-eligible attempt at all: nothing could ever publish a result.
    """
    coordinator = Coordinator()
    aggregate = coordinator.create_request(
        "R1", "t1", "i1", "sha256:d", max_attempts=3
    )
    coordinator.open_attempt("R1", "A1", "res-1", "provider-A")
    decision = _retry(coordinator, "R1", "A1", "A2")

    assert eligibility(aggregate) == ["A2"], f"decision was {decision}"
    assert aggregate.attempts["A1"].superseded_by == "A2"
    # The new branch must actually be able to publish.
    assert coordinator.commit_output("R1", "A2", 1, "gen-A2") is True


def eligibility(aggregate: object) -> list[str]:
    """Return the ids of output-eligible attempts, sorted."""
    return sorted(
        a.attempt_id for a in aggregate.attempts.values() if a.output_eligible  # type: ignore[attr-defined]
    )


def test_retry_advances_the_dispatch_epoch() -> None:
    """A failover is an event that must be visible in the epoch."""
    coordinator = Coordinator()
    aggregate = coordinator.create_request(
        "R1", "t1", "i1", "sha256:d", max_attempts=3
    )
    coordinator.open_attempt("R1", "A1", "res-1", "provider-A")
    before = aggregate.request.dispatch_epoch
    _retry(coordinator, "R1", "A1", "A2")
    assert aggregate.request.dispatch_epoch == before + 1
    assert aggregate.attempts["A2"].dispatch_epoch == aggregate.request.dispatch_epoch


def test_retry_still_respects_the_attempt_budget() -> None:
    """The budget still binds on the retry path."""
    coordinator = Coordinator()
    aggregate = coordinator.create_request(
        "R1", "t1", "i1", "sha256:d", max_attempts=2
    )
    coordinator.open_attempt("R1", "A1", "res-1", "provider-A")
    _retry(coordinator, "R1", "A1", "A2")
    # A2 filled the budget; a further retry finds the factory refused.
    decision = _retry(coordinator, "R1", "A2", "A3")
    assert decision.action == "fail_explicitly"
    assert "A3" not in aggregate.attempts


# -- settlement error surface -------------------------------------------------


def _engine() -> SettlementEngine:
    return SettlementEngine(
        ledger=Ledger(), registry=IdentityRegistry(), log=MerkleLog()
    )


def _record(schema: str, payload: dict[str, object], signer: str) -> SignedRecord:
    return SignedRecord(
        schema=schema,
        record_id="rec",
        revision=1,
        signed_at=SETTLEMENT_AT,
        signer_key_id="k",
        signer_id=signer,
        signature="AAAA",
        payload=payload,
    )


def test_malformed_report_raises_a_settlement_error_not_a_key_error() -> None:
    """An unauthenticated caller must not provoke a non-domain exception.

    The settlement fingerprint reads the report's token total before signature
    validation, so a report missing a field must still fail with the documented
    error type rather than a bare `KeyError`.
    """
    engine = _engine()
    malformed = _record(
        SCHEMA_USAGE,
        {
            "measurement": {"attempt_id": "A1", "usage": {}},
            "device_id": "device-B",
            "provider_id": "provider-B",
        },
        "device-B",
    )
    authorization = _record(
        SCHEMA_AUTHORIZATION, {"payer_account_id": "consumer-X"}, "consumer-X"
    )
    with pytest.raises(SettlementError):
        engine.settle(
            request_id="R-1",
            service_unit_id="S-1",
            authorization=authorization,
            report=malformed,
            contract=CONTRACT,
            contract_hash="sha256:c",
            settlement_private_key=object(),
            winner_attempt_id="A1",
        )


def test_caller_cannot_poison_the_cached_allocation(ledger: Ledger, keyring: Keyring) -> None:
    """Mutating a returned result must not corrupt the cache or the ledger."""
    engine = SettlementEngine(ledger=ledger, registry=keyring.registry, log=MerkleLog())
    authorization = signed_authorization(keyring, request_id="R-1001")
    report = _signed_report(keyring)

    first = engine.settle(
        request_id="R-1001",
        service_unit_id="S-1",
        authorization=authorization,
        report=report,
        contract=CONTRACT,
        contract_hash=contract_hash(CONTRACT),
        settlement_private_key=keyring.private["settlement-key-1"],
        winner_attempt_id="A2",
        settled_at=SETTLEMENT_AT,
    )
    expected = dict(first.allocations)
    first.allocations["provider-B"] = -999_999

    replay = engine.settle(
        request_id="R-1001",
        service_unit_id="S-1",
        authorization=authorization,
        report=report,
        contract=CONTRACT,
        contract_hash=contract_hash(CONTRACT),
        settlement_private_key=keyring.private["settlement-key-1"],
        winner_attempt_id="A2",
        settled_at=SETTLEMENT_AT,
    )
    assert replay.allocations == expected
    assert ledger.balance("provider-B/pending") == sum(expected.values())


def test_replay_returns_the_same_receipt_and_log_position(
    ledger: Ledger, keyring: Keyring
) -> None:
    """One payment, one receipt, one log leaf, however many attempts."""
    engine = SettlementEngine(ledger=ledger, registry=keyring.registry, log=MerkleLog())
    authorization = signed_authorization(keyring, request_id="R-1001")
    report = _signed_report(keyring)
    kwargs = {
        "request_id": "R-1001",
        "service_unit_id": "S-1",
        "authorization": authorization,
        "report": report,
        "contract": CONTRACT,
        "contract_hash": contract_hash(CONTRACT),
        "settlement_private_key": keyring.private["settlement-key-1"],
        "winner_attempt_id": "A2",
        "settled_at": SETTLEMENT_AT,
    }
    first = engine.settle(**kwargs)  # type: ignore[arg-type]
    second = engine.settle(**kwargs)  # type: ignore[arg-type]
    assert first.receipt.hash == second.receipt.hash
    assert first.log_index == second.log_index
    assert engine.log.size() == 1
    assert len(engine.outbox()) == 1


def test_conflicting_replay_under_one_business_key_is_refused(
    ledger: Ledger, keyring: Keyring
) -> None:
    """Different economic content under one (unit, revision) is a conflict."""
    from aipotluck.errors import ConflictError

    engine = SettlementEngine(ledger=ledger, registry=keyring.registry, log=MerkleLog())
    authorization = signed_authorization(keyring, request_id="R-1001")
    report = _signed_report(keyring)
    engine.settle(
        request_id="R-1001",
        service_unit_id="S-1",
        authorization=authorization,
        report=report,
        contract=CONTRACT,
        contract_hash=contract_hash(CONTRACT),
        settlement_private_key=keyring.private["settlement-key-1"],
        winner_attempt_id="A2",
        settled_at=SETTLEMENT_AT,
    )
    with pytest.raises(ConflictError):
        engine.settle(
            request_id="R-1001",
            service_unit_id="S-1",
            authorization=authorization,
            report=report,
            contract=CONTRACT,
            contract_hash=contract_hash(CONTRACT),
            settlement_private_key=keyring.private["settlement-key-1"],
            winner_attempt_id="A2",
            billable_tokens=1,  # a different amount for the same unit
            settled_at=SETTLEMENT_AT,
        )


def _signed_report(keyring: Keyring) -> SignedRecord:
    from aipotluck.records import UsageReport

    measurement = Measurement(
        measurement_id="m-1",
        scope="upstream_call",
        semantics="terminal_total",
        counter_epoch="epoch_1",
        revision=1,
        usage=NormalizedUsage(input_total=800, output_total=200),
        attempt_id="A2",
    )
    return UsageReport(
        report_id="rep-1",
        permit_id="permit-1",
        permit_hash="permit-hash",
        acceptance_hash="acceptance-hash",
        attempt_id="A2",
        provider_id="provider-B",
        device_id="device-B",
        measurement=measurement,
        result_commitment=None,
        final_event_seq=1,
        outcome="completed",
        reported_at=SETTLEMENT_AT,
        key_id="device-key-B",
    ).sign(keyring.private["device-key-B"], "device-key-B")


# -- spend-grant lifecycle ----------------------------------------------------


def test_a_grant_can_be_replaced_after_revocation() -> None:
    """Revoke-then-grant must actually work, as the API documents."""
    registry = IdentityRegistry()
    registry.grant_spend_authority(
        "payer", "spender", SETTLEMENT_AT - dt.timedelta(hours=2), max_milli_credits=100
    )
    registry.revoke_spend_authority("payer", "spender", SETTLEMENT_AT - dt.timedelta(hours=1))
    registry.grant_spend_authority(
        "payer", "spender", SETTLEMENT_AT - dt.timedelta(minutes=30), max_milli_credits=9_000
    )
    # The replacement is in force and carries the new cap.
    registry.check_spend_authority("spender", "payer", 9_000, SETTLEMENT_AT)
    with pytest.raises(IdentityBindingError, match="caps a charge"):
        registry.check_spend_authority("spender", "payer", 9_001, SETTLEMENT_AT)


def test_grant_is_found_by_the_account_pair_alone() -> None:
    """A grant is addressable by (payer, spender), with no extra identifier."""
    registry = IdentityRegistry()
    grant = registry.grant_spend_authority(
        "payer", "spender", SETTLEMENT_AT - dt.timedelta(hours=1)
    )
    assert grant.grant_id == "payer<-spender"
    registry.check_spend_authority("spender", "payer", 1, SETTLEMENT_AT)
    registry.revoke_spend_authority("payer", "spender", SETTLEMENT_AT)
    with pytest.raises(IdentityBindingError, match="not in force"):
        registry.check_spend_authority(
            "spender", "payer", 1, SETTLEMENT_AT + dt.timedelta(hours=1)
        )


def test_ledger_engine_counters_are_not_reused_across_engines(
    ledger: Ledger, keyring: Keyring
) -> None:
    """A second engine over the same ledger must not reuse an existing journal id.

    Guards the failure mode where a restarted engine numbers its first journal
    J-1 while the ledger already holds a J-1 for another business key.
    """
    first_engine = SettlementEngine(
        ledger=ledger, registry=keyring.registry, log=MerkleLog()
    )
    authorization = signed_authorization(keyring, request_id="R-1001")
    report = _signed_report(keyring)
    first_engine.settle(
        request_id="R-1001",
        service_unit_id="S-1",
        authorization=authorization,
        report=report,
        contract=CONTRACT,
        contract_hash=contract_hash(CONTRACT),
        settlement_private_key=keyring.private["settlement-key-1"],
        winner_attempt_id="A2",
        settled_at=SETTLEMENT_AT,
    )
    existing_ids = {j.journal_id for j in ledger.journals()}

    second_engine = SettlementEngine(
        ledger=ledger, registry=keyring.registry, log=MerkleLog()
    )
    result = second_engine.settle(
        request_id="R-1002",
        service_unit_id="S-2",
        authorization=signed_authorization(keyring, request_id="R-1002"),
        report=report,
        contract=CONTRACT,
        contract_hash=contract_hash(CONTRACT),
        settlement_private_key=keyring.private["settlement-key-1"],
        winner_attempt_id="A2",
        settled_at=SETTLEMENT_AT,
    )
    assert result.journal.journal_id not in existing_ids
    assert dataclasses.is_dataclass(result.journal)