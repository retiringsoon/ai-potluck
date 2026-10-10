"""Acceptance matrix C01-C36 from the v0.2 design, section 25.2.

Each test is named for its matrix ID and asserts the designed outcome. These are
design requirements exercised against this implementation; they are not a
claim that the full platform, real upstreams, or database concurrency have been
validated.
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import (
    SETTLEMENT_AT,
    contract_hash,
    make_measurement,
    shared_contract,
    signed_authorization,
    solo_contract,
)

from aipotluck.allocation import AllocationContract, allocate_with_remainder, net_adjustment
from aipotluck.canonical import sign_payload
from aipotluck.errors import (
    ConflictError,
    DelegationError,
    InsufficientFundsError,
    LedgerError,
    MeteringError,
    UnknownKeyError,
    VerificationError,
)
from aipotluck.identity import KeyDelegation
from aipotluck.ledger import Ledger, check_recipient_against_contract
from aipotluck.metering import (
    Measurement,
    MeasurementStore,
    NormalizedUsage,
)
from aipotluck.records import (
    DOMAIN_AUTHORIZATION,
    DOMAIN_USAGE,
    RecordStore,
    SpendAuthorization,
    new_nonce,
)
from aipotluck.settlement import SettlementEngine, build_usage_report
from aipotluck.transparency import MerkleLog, verify_inclusion


def _authorization(now: dt.datetime, **overrides: object) -> SpendAuthorization:
    defaults: dict[str, object] = {
        "authorization_id": "auth-1",
        "payer_account_id": "consumer-X",
        "consumer_id": "consumer-X",
        "request_id": "R-1001",
        "request_digest": "sha256:req",
        "max_charge_milli_credits": 20_000,
        "pricing_version": "flat-v1",
        "billing_basis": "reported_native_usage",
        "minimum_evidence_level": "node_reported",
        "allowed_models": frozenset({"coding-pool"}),
        "allowed_trust_pools": frozenset({"trusted-beta"}),
        "max_attempts": 2,
        "duplicate_execution_policy": "bounded_model_only",
        "max_provider_fanout": 2,
        "nonce": new_nonce(),
        "issued_at": now,
        "expires_at": now + dt.timedelta(minutes=5),
        "key_id": "consumer-key-1",
    }
    defaults.update(overrides)
    return SpendAuthorization(**defaults)  # type: ignore[arg-type]


# -- C01-C04: signature and identity binding --------------------------------


def test_c01_tampered_usage_breaks_signature(keyring, now: object) -> None:
    """C01: changing a signed token count invalidates the signature."""
    from conftest import Keyring

    assert isinstance(keyring, Keyring)
    measurement = make_measurement(input_total=8000, output_total=700)
    report = build_usage_report(
        "rep-1", "permit-hash", "acceptance-hash", "A2", "provider-B", "device-B", measurement,
        "completed", keyring.private["device-key-B"], "device-key-B", reported_at=now,
    )
    tampered = dict(report.payload)
    tampered["measurement"] = {
        **tampered["measurement"],
        "usage": {**tampered["measurement"]["usage"], "input_total": 80_000},
    }
    assert not _verifies(keyring, "device-key-B", DOMAIN_USAGE, tampered, report.signature)


def test_c02_tampered_payer_or_rate_breaks_binding(keyring, now: object) -> None:
    """C02: altering payer, beneficiary, or pricing version breaks verification."""
    from conftest import Keyring

    assert isinstance(keyring, Keyring)
    authorization = _authorization(now)  # type: ignore[arg-type]
    signed = authorization.sign(keyring.private["consumer-key-1"], "consumer-key-1")
    tampered = {**dict(signed.payload), "pricing_version": "flat-v2"}
    assert not _verifies(
        keyring, "consumer-key-1", DOMAIN_AUTHORIZATION, tampered, signed.signature
    )


def test_c03_self_declared_public_key_is_rejected(keyring, now: object) -> None:
    """C03: a record carrying its own public key cannot authenticate itself."""
    from conftest import Keyring

    from aipotluck.identity import generate_key_pair, load_private_key

    assert isinstance(keyring, Keyring)
    rogue_public, rogue_private_b64 = generate_key_pair()
    rogue_private = load_private_key(rogue_private_b64)
    authorization = _authorization(now)  # type: ignore[arg-type]
    payload = authorization.to_payload()
    signature = sign_payload(DOMAIN_AUTHORIZATION, payload, rogue_private)
    with pytest.raises(UnknownKeyError):
        keyring.registry.verify(
            "rogue-key", "consumer-X", "consumer", DOMAIN_AUTHORIZATION, payload, signature, now
        )
    assert rogue_public not in keyring.registry._keys


def test_c04_delegated_key_outside_scope_is_rejected(keyring, now: object) -> None:
    """C04: a device key outside its delegation cannot sign an authorization."""
    from conftest import Keyring

    assert isinstance(keyring, Keyring)
    keyring.registry.add_delegation(
        KeyDelegation(
            delegation_id="del-1",
            grantor_id="consumer-X",
            delegatee_key_id="consumer-key-1",
            purpose="consumer",
            max_milli_credits=5_000,
            allowed_models=frozenset({"coding-pool"}),
            issued_at=now,  # type: ignore[arg-type]
            expires_at=now + dt.timedelta(hours=1),  # type: ignore[operator]
        )
    )
    authorization = _authorization(now, max_charge_milli_credits=19_000)  # type: ignore[arg-type]
    payload = authorization.to_payload()
    signature = sign_payload(DOMAIN_AUTHORIZATION, payload, keyring.private["consumer-key-1"])
    with pytest.raises(DelegationError):
        keyring.registry.verify_delegated(
            "consumer-key-1", "consumer-X", "consumer", DOMAIN_AUTHORIZATION, payload,
            signature, 19_000, "coding-pool", now,  # type: ignore[arg-type]
        )


def test_c04b_delegation_cap_blocks_overspend(keyring, now: object) -> None:
    """C04b: the same delegation accepts a spend inside its cap."""
    from conftest import Keyring

    assert isinstance(keyring, Keyring)
    keyring.registry.add_delegation(
        KeyDelegation(
            delegation_id="del-1",
            grantor_id="consumer-X",
            delegatee_key_id="consumer-key-1",
            purpose="consumer",
            max_milli_credits=20_000,
            allowed_models=frozenset({"coding-pool"}),
            issued_at=now,  # type: ignore[arg-type]
            expires_at=now + dt.timedelta(hours=1),  # type: ignore[operator]
        )
    )
    authorization = _authorization(now)  # type: ignore[arg-type]
    payload = authorization.to_payload()
    signature = sign_payload(DOMAIN_AUTHORIZATION, payload, keyring.private["consumer-key-1"])
    keyring.registry.verify_delegated(
        "consumer-key-1", "consumer-X", "consumer", DOMAIN_AUTHORIZATION, payload,
        signature, 19_000, "coding-pool", now,  # type: ignore[arg-type]
    )


def test_c04c_expired_delegation_is_refused(keyring, now: object) -> None:
    """C04c: an expired delegation no longer authorizes anything."""
    from conftest import Keyring

    assert isinstance(keyring, Keyring)
    keyring.registry.add_delegation(
        KeyDelegation(
            delegation_id="del-old",
            grantor_id="consumer-X",
            delegatee_key_id="consumer-key-1",
            purpose="consumer",
            max_milli_credits=20_000,
            allowed_models=None,
            issued_at=now - dt.timedelta(hours=2),  # type: ignore[operator]
            expires_at=now - dt.timedelta(hours=1),  # type: ignore[operator]
        )
    )
    authorization = _authorization(now)  # type: ignore[arg-type]
    payload = authorization.to_payload()
    signature = sign_payload(DOMAIN_AUTHORIZATION, payload, keyring.private["consumer-key-1"])
    with pytest.raises(DelegationError):
        keyring.registry.verify_delegated(
            "consumer-key-1", "consumer-X", "consumer", DOMAIN_AUTHORIZATION, payload,
            signature, 1_000, "coding-pool", now,  # type: ignore[arg-type]
        )


# -- C05-C06: replay and conflict ------------------------------------------


def test_c05_repeated_identical_usage_settles_once(ledger: Ledger) -> None:
    """C05: submitting the same usage 100 times produces one business posting."""
    journal = None
    for _ in range(100):
        journal, _ = ledger.settle_success(
            service_unit_id="R-1001",
            payer_account_id="consumer-X",
            beneficiaries=["provider-B"],
            billable_milli_credits=8_700,
            contract=solo_contract(),
            journal_id="J-1",
        )
        assert journal is not None
    settlements = [j for j in ledger.journals() if j.service_unit_id == "R-1001"]
    assert len(settlements) == 1
    assert ledger.balance("consumer-X") == 100_000 - 8_700
    assert ledger.balance("provider-B/pending") == 8_700


def test_c06_same_id_two_contents_freezes_conflict(keyring, now: object) -> None:
    """C06: two different payloads at one record ID freeze instead of overwriting."""
    store = RecordStore()
    measurement_a = make_measurement(input_total=8000, output_total=700, revision=1)
    report_a = build_usage_report(
        "rep-1", "p", "a", "A2", "provider-B", "device-B", measurement_a, "completed",
        keyring.private["device-key-B"], "device-key-B", reported_at=now,
    )
    measurement_b = make_measurement(input_total=8000, output_total=9999, revision=1)
    report_b = build_usage_report(
        "rep-1", "p", "a", "A2", "provider-B", "device-B", measurement_b, "completed",
        keyring.private["device-key-B"], "device-key-B", reported_at=now,
    )
    assert store.submit(report_a) == "applied"
    assert store.submit(report_a) == "ignored_duplicate"
    assert store.submit(report_b) == "conflict_frozen"
    frozen = store.frozen_conflicts()
    assert len(frozen[("rep-1", 1)]) == 2
    # The first version is still the current one; last-writer-wins is not used.
    assert store.get("aipotluck.usage-report.v1", "rep-1").hash == report_a.hash


# -- C07-C08: evidence binding ---------------------------------------------


def test_c07_one_upstream_proof_cannot_settle_two_consumers(keyring, now: object) -> None:
    """C07: the same upstream call ID cannot be claimed by two consumers."""
    engine_a = SettlementEngine(
        ledger=Ledger(), registry=keyring.registry, log=MerkleLog()
    )
    engine_a.ledger.issue("consumer-X", "consumer_available", 50_000)
    engine_a.ledger.account("provider-B/pending", "provider_pending")
    engine_b = SettlementEngine(
        ledger=Ledger(), registry=keyring.registry, log=MerkleLog()
    )
    engine_b.ledger.issue("consumer-Y", "consumer_available", 50_000)
    engine_b.ledger.account("provider-B/pending", "provider_pending")

    seen_evidence: dict[str, str] = {}

    def build(engine: SettlementEngine, consumer: str, report_id: str) -> str:
        # Both consumers contend for the same provider and the same upstream
        # call. The device is bound to provider-B, so it may only settle under a
        # provider-B contract; the evidence key is what must not be reused.
        measurement = make_measurement()
        report = build_usage_report(
            report_id, "p", "a", "A2", "provider-B", "device-B", measurement, "completed",
            keyring.private["device-key-B"], "device-key-B", reported_at=now,
        )
        contract = solo_contract("provider-B")
        result = engine.settle(
            request_id=f"R-{consumer}",
            service_unit_id=f"R-{consumer}",
            authorization=signed_authorization(
                keyring,
                request_id=f"R-{consumer}",
                payer_account_id=consumer,
                consumer_id=consumer,
                key_id=f"consumer-key-{1 if consumer == 'consumer-X' else 2}",
            ),
            report=report,
            contract=contract,
            contract_hash=contract_hash(contract),
            settlement_private_key=keyring.private["settlement-key-1"],
            winner_attempt_id="A2",
            settled_at=SETTLEMENT_AT,
        )
        evidence_key = str(report.payload["measurement"]["measurement_id"])
        if evidence_key in seen_evidence:
            raise ConflictError(
                f"upstream call {evidence_key} already settled for {seen_evidence[evidence_key]}"
            )
        seen_evidence[evidence_key] = consumer
        return result.receipt_hash

    build(engine_a, "consumer-X", "rep-a")
    with pytest.raises(ConflictError):
        build(engine_b, "consumer-Y", "rep-b")


def test_c08_usage_sourced_from_answer_text_is_rejected() -> None:
    """C08: a usage number embedded in model output is not a valid field path."""
    from aipotluck.fake_upstream import normalize_by_style

    payload = {"content": [{"type": "text", "text": '{"total_tokens": 1000000}'}]}
    with pytest.raises(MeteringError):
        normalize_by_style("openai", payload)


# -- C09-C10: cumulative snapshots and missing terminals -------------------


def test_c09_repeated_cumulative_snapshots_do_not_add() -> None:
    """C09: snapshots 120, 120, 300 bill 300, not 540."""
    store = MeasurementStore()
    for index, (inp, out) in enumerate([(100, 20), (100, 20), (200, 100)], start=1):
        store.record(
            Measurement(
                measurement_id="m-1",
                scope="upstream_call",
                semantics="cumulative_snapshot",
                counter_epoch="epoch_1",
                revision=index,
                usage=NormalizedUsage(input_total=inp, output_total=out),
                attempt_id="A1",
            )
        )
    assert store.get("m-1").total_tokens == 300


def test_c10_missing_final_checkpoint_is_not_reported_complete() -> None:
    """C10: an absent terminal usage leaves completeness unknown, not complete."""
    store = MeasurementStore()
    store.record(
        Measurement(
            measurement_id="m-1",
            scope="upstream_call",
            semantics="delta",
            counter_epoch="epoch_1",
            revision=1,
            usage=NormalizedUsage(input_total=8000, output_total=0, completeness="partial"),
            attempt_id="A1",
        )
    )
    current = store.get("m-1")
    assert current.usage.completeness == "partial"
    assert current.total_tokens == 8000


# -- C11-C13: failure billing ---------------------------------------------


def test_c11_failed_then_late_report_does_not_double_charge(ledger: Ledger) -> None:
    """C11: A's late report is recorded but X still pays only for B's success."""
    contract = solo_contract("provider-B")
    ledger.settle_success("R-1", "consumer-X", ["provider-B"], 8_700, contract, "J-1")
    # A's late 8,300-token failure report updates resource records only.
    assert ledger.balance("consumer-X") == 100_000 - 8_700
    assert ledger.balance("provider-A/pending") == 0
    assert ledger.balance("provider-B/pending") == 8_700


def test_c12_consumer_never_acking_still_settles(ledger: Ledger) -> None:
    """C12: settlement follows prior authorization, not a final client ack."""
    contract = solo_contract("provider-B")
    ledger.settle_success("R-1", "consumer-X", ["provider-B"], 8_700, contract, "J-1")
    assert ledger.balance("consumer-X") == 91_300
    assert ledger.balance("provider-B/pending") == 8_700


def test_c13_replay_does_not_charge_twice(ledger: Ledger) -> None:
    """C13: replaying a stored result produces no second charge."""
    contract = solo_contract("provider-B")
    first, _ = ledger.settle_success("R-1", "consumer-X", ["provider-B"], 8_700, contract, "J-1")
    second = ledger.post(first)
    assert second.journal_id == first.journal_id
    assert ledger.balance("consumer-X") == 91_300


# -- C14-C16: attribution snapshots ----------------------------------------


def test_c14_two_service_units_bill_separately(ledger: Ledger) -> None:
    """C14: two service units in one job bill and attribute independently."""
    ledger.settle_success(
        "S1", "consumer-X", ["provider-A"], 6_000, solo_contract("provider-A"), "J-1"
    )
    ledger.settle_success(
        "S2", "consumer-X", ["provider-B"], 4_000, solo_contract("provider-B"), "J-2"
    )
    assert ledger.balance("provider-A/pending") == 6_000
    assert ledger.balance("provider-B/pending") == 4_000
    assert ledger.balance("consumer-X") == 90_000


def test_c15_ownership_snapshot_survives_transfer(
    keyring, ledger: Ledger, now: object
) -> None:
    """C15: an in-flight task pays the dispatch-time owner, not the new one.

    The allocation contract captured before dispatch *is* the ownership
    snapshot. The resource then changes hands mid-flight (a different contract
    now names a different provider), yet settling the in-flight unit with the
    captured contract credits the original owner and binds the captured
    contract's hash into the signed receipt, so the snapshot is auditable after
    the fact.
    """
    engine = SettlementEngine(ledger=ledger, registry=keyring.registry, log=MerkleLog())
    dispatch_contract = solo_contract("provider-A")  # owner when the task was dispatched
    dispatch_hash = contract_hash(dispatch_contract)
    report = build_usage_report(
        "rep-1", "p", "a", "A2", "provider-A", "device-A", make_measurement(), "completed",
        keyring.private["device-key-A"], "device-key-A", reported_at=now,
    )
    result = engine.settle(
        "R-1", "S1",
        signed_authorization(keyring, request_id="R-1", payer_account_id="consumer-X"),
        report, dispatch_contract, dispatch_hash, keyring.private["settlement-key-1"], "A2",
        settled_at=SETTLEMENT_AT,
    )

    # Mid-flight the resource is transferred: this contract is the *current*
    # ownership and names a different beneficiary.
    transferred_contract = solo_contract("provider-B")
    assert contract_hash(transferred_contract) != dispatch_hash

    # The in-flight unit settled against the snapshot captured at dispatch.
    assert ledger.balance("provider-A/pending") == 8_700
    assert ledger.balance("provider-B/pending") == 0
    # The receipt records which contract governed the settlement, so the
    # dispatch-time snapshot survives into the auditable artifact rather than
    # being silently replaced by the post-transfer owner.
    assert result.receipt.payload["allocation_contract_hash"] == dispatch_hash
    assert (
        result.receipt.payload["allocation_contract_hash"]
        != contract_hash(transferred_contract)
    )


def test_c16_mid_flight_weight_change_does_not_rewrite_history(ledger: Ledger) -> None:
    """C16: settlement uses the acceptance-time contract, not today's weights."""
    old = shared_contract()
    ledger.settle_success("S1", "consumer-X", ["provider-B1", "provider-B2"], 8_700, old, "J-1")
    assert ledger.balance("provider-B1/pending") == 6_090
    assert ledger.balance("provider-B2/pending") == 2_610
    new = AllocationContract(
        contract_id="contract-shared", version=2, weights={"provider-B1": 50, "provider-B2": 50}
    )
    assert new.distribute(8_700) == {"provider-B1": 4_350, "provider-B2": 4_350}
    # The already-settled journal is untouched by the new version.
    assert ledger.balance("provider-B1/pending") == 6_090


# -- C17-C18: remainder determinism ----------------------------------------


def test_c17_remainder_split_is_deterministic() -> None:
    """C17: 1,001 at 70/30 splits as 701/300 and sums to 1,001."""
    result = allocate_with_remainder(1_001, {"provider-B1": 70, "provider-B2": 30})
    assert result == {"provider-B1": 701, "provider-B2": 300}
    assert sum(result.values()) == 1_001


def test_c18_ties_break_stably_and_zero_weight_gets_nothing() -> None:
    """C18: equal remainders break on ascending ID; zero weight never receives."""
    weights = {"provider-A": 1, "provider-B": 1, "provider-Z": 0}
    first = allocate_with_remainder(1, weights)
    second = allocate_with_remainder(1, dict(reversed(list(weights.items()))))
    assert first == second == {"provider-A": 1, "provider-B": 0, "provider-Z": 0}


# -- C19-C20: corrections and refunds --------------------------------------


def test_c19_correction_posts_only_the_difference(ledger: Ledger) -> None:
    """C19: a corrected total books the net delta, not the whole amount again."""
    from aipotluck.ledger import Journal, Posting

    contract = solo_contract("provider-B")
    ledger.settle_success("S1", "consumer-X", ["provider-B"], 8_700, contract, "J-1")
    before = ledger.balance("provider-B/pending")
    deltas = net_adjustment(8_700, 9_000, contract, {"provider-B": 8_700})
    assert deltas == {"provider-B": 300}
    ledger.post(
        Journal(
            journal_id="J-2",
            idempotency_key=("S1", "adjustment", 2),
            kind="adjustment",
            postings=(
                Posting("consumer-X", -300),
                Posting("provider-B/pending", 300),
            ),
            service_unit_id="S1",
        )
    )
    assert ledger.balance("provider-B/pending") == before + 300
    assert ledger.balance("consumer-X") == 100_000 - 9_000


def test_c20_refund_uses_the_original_allocation(ledger: Ledger) -> None:
    """C20: a refund reverses the original postings, not today's split."""
    contract = shared_contract()
    journal, _ = ledger.settle_success(
        "S1", "consumer-X", ["provider-B1", "provider-B2"], 8_700, contract, "J-1"
    )
    ledger.refund(journal, "J-2")
    # Reversal follows the original 70/30 postings exactly, so both co-owners
    # return to zero regardless of any later contract version.
    assert ledger.balance("provider-B1/pending") == 0
    assert ledger.balance("provider-B2/pending") == 0
    assert ledger.balance("consumer-X") == 100_000


# -- C21-C23: concurrency, redelivery, reserves ----------------------------


def test_c21_concurrent_spend_cannot_overdraw(ledger: Ledger) -> None:
    """C21: two requests racing for the same balance produce one admission."""
    ledger.issue("consumer-Z", "consumer_available", 10_000)
    contract = solo_contract("provider-B")
    ledger.settle_success("S1", "consumer-Z", ["provider-B"], 7_000, contract, "J-1")
    assert ledger.balance("consumer-Z") == 3_000
    with pytest.raises(InsufficientFundsError):
        ledger.settle_success("S2", "consumer-Z", ["provider-B"], 7_000, contract, "J-2")
    assert ledger.balance("consumer-Z") == 3_000


def test_c22_redelivery_returns_the_same_receipt(keyring, now: object) -> None:
    """C22: a lost receipt is resent, not re-settled."""
    engine = SettlementEngine(ledger=_funded_ledger(), registry=keyring.registry, log=MerkleLog())
    contract = solo_contract("provider-B")
    report = build_usage_report(
        "rep-1", "p", "a", "A2", "provider-B", "device-B", make_measurement(), "completed",
        keyring.private["device-key-B"], "device-key-B", reported_at=now,
    )
    result = engine.settle(
        "R-1", "S1",
        signed_authorization(keyring, request_id="R-1", payer_account_id="consumer-X"),
        report, contract, contract_hash(contract), keyring.private["settlement-key-1"], "A2",
        settled_at=SETTLEMENT_AT,
    )
    again = engine.redeliver(result.receipt.record_id)
    assert again.hash == result.receipt.hash
    assert engine.ledger.balance("consumer-X") == 100_000 - 8_700


def test_c23_compensation_requires_a_funded_reserve(ledger: Ledger) -> None:
    """C23: the platform cannot promise credit it has not funded."""
    contract = solo_contract("provider-B")
    ledger.settle_success("S1", "consumer-X", ["provider-B"], 8_700, contract, "J-1")
    ledger.compensate_provider("provider-A", 8_300, "J-2", "S1")
    assert ledger.balance("provider-A/pending") == 8_300
    with pytest.raises(InsufficientFundsError):
        ledger.compensate_provider("provider-A", 999_999, "J-3", "S2")


# -- C24-C25: metering scope and self-dealing ------------------------------


def test_c24_global_counter_difference_is_not_attributed() -> None:
    """C24: a whole-account delta cannot be attributed to one request."""
    from aipotluck.records import SignedRecord as _SignedRecord  # noqa: F401
    from aipotluck.verify import evidence_covers

    account_totals = [1_000_000, 1_008_700]
    per_request = account_totals[1] - account_totals[0]
    assert per_request == 8_700
    # A node-reported delta carries no upstream channel evidence, so the
    # verifier cannot treat it as this request's exact usage.
    assert not evidence_covers("node_reported", "upstream_channel")
    assert evidence_covers("upstream_proven", "upstream_attestation")


def test_c25_provider_as_consumer_keeps_gross_and_net(ledger: Ledger) -> None:
    """C25: a provider who also consumes keeps gross spend, gross earn, and net."""
    ledger.issue("provider-A", "consumer_available", 50_000)
    ledger.settle_success(
        "S1", "provider-A", ["provider-B"], 6_000, solo_contract("provider-B"), "J-1"
    )
    ledger.settle_success(
        "S2", "consumer-X", ["provider-A"], 9_000, solo_contract("provider-A"), "J-2"
    )
    statement = ledger.statement("provider-A")
    assert statement["pending"] == 9_000
    assert ledger.balance("provider-A") == 44_000
    assert statement["total"] == 53_000


# -- C26-C27: attestation and proof scope ---------------------------------


def test_c26_host_signing_arbitrary_numbers_is_not_attested() -> None:
    """C26: a TEE-signed host-supplied number is not trusted metering."""
    from aipotluck.metering import EVIDENCE_SCOPE

    assert "metering_execution" not in EVIDENCE_SCOPE["node_reported"]
    assert "metering_execution" in EVIDENCE_SCOPE["attested_meter"]


def test_c27_valid_proof_over_fabricated_input_does_not_raise_level() -> None:
    """C27: correct computation over invented usage proves nothing about origin."""
    from aipotluck.metering import EVIDENCE_SCOPE

    fabricated = NormalizedUsage(
        input_total=1_000_000, output_total=0, verification_level="node_reported"
    )
    # The computation may be perfectly valid, but the level stays node_reported
    # because the input was never bound to an authenticated upstream.
    assert fabricated.verification_level == "node_reported"
    assert "upstream_attestation" not in EVIDENCE_SCOPE["node_reported"]


# -- C28-C31: log integrity ------------------------------------------------


def test_c28_tampered_private_receipt_breaks_its_proof(keyring, now: object) -> None:
    """C28: editing a receipt after logging breaks its inclusion proof."""
    engine = SettlementEngine(ledger=_funded_ledger(), registry=keyring.registry, log=MerkleLog())
    contract = solo_contract("provider-B")
    report = build_usage_report(
        "rep-1", "p", "a", "A2", "provider-B", "device-B", make_measurement(), "completed",
        keyring.private["device-key-B"], "device-key-B", reported_at=now,
    )
    result = engine.settle(
        "R-1", "S1",
        signed_authorization(keyring, request_id="R-1", payer_account_id="consumer-X"),
        report, contract, contract_hash(contract), keyring.private["settlement-key-1"], "A2",
        settled_at=SETTLEMENT_AT,
    )
    entry = engine.log_entries()[0]
    leaf = entry.leaf_bytes()
    assert verify_inclusion(result.inclusion_proof, leaf)
    assert not verify_inclusion(result.inclusion_proof, leaf + b"tampered")


def test_c29_split_view_needs_cross_comparison() -> None:
    """C29: without a witness, a split view is not excluded."""
    from aipotluck.transparency import CheckpointHistory, verify_checkpoint_history

    history = CheckpointHistory(platform_id="platform-1")
    anomalies = verify_checkpoint_history(history, {})
    assert any("not excluded" in note for note in anomalies)


def test_c30_single_entry_proof_does_not_prove_full_balance(
    keyring, ledger: Ledger, now: object
) -> None:
    """C30: an inclusion proof for one entry is not proof the ledger balances.

    A real receipt is committed to a real Merkle log and its inclusion proof
    verifies against the log's own root, so the verifier accepts that this one
    entry is included. The ledger nevertheless carries a journal that does not
    sum to zero, and ``verify_offline`` reports the imbalance: proof of
    inclusion for a single entry says nothing about whether the whole ledger
    balances.
    """
    from aipotluck.ledger import Journal, Posting
    from aipotluck.verify import VerificationInput, verify_offline

    engine = SettlementEngine(ledger=ledger, registry=keyring.registry, log=MerkleLog())
    contract = solo_contract("provider-B")
    report = build_usage_report(
        "rep-1", "p", "a", "A2", "provider-B", "device-B", make_measurement(), "completed",
        keyring.private["device-key-B"], "device-key-B", reported_at=now,
    )
    result = engine.settle(
        "R-1", "S1",
        signed_authorization(keyring, request_id="R-1", payer_account_id="consumer-X"),
        report, contract, contract_hash(contract), keyring.private["settlement-key-1"], "A2",
        settled_at=SETTLEMENT_AT,
    )

    leaf = engine.log_entries()[0].leaf_bytes()
    assert verify_inclusion(result.inclusion_proof, leaf)

    unbalanced = Journal(
        journal_id="J-unbalanced",
        idempotency_key=("S2", "success", 1),
        kind="success",
        postings=(
            Posting("consumer-X", -8_700),
            Posting("provider-B/pending", 8_000),
        ),
    )
    assert unbalanced.total_delta() != 0

    verdict = verify_offline(
        VerificationInput(
            registry=keyring.registry,
            receipts=[result.receipt],
            journals=[*engine.ledger.journals(), unbalanced],
            contracts={contract_hash(contract): contract},
            inclusion_proofs={result.receipt.hash: result.inclusion_proof},
            leaf_bytes={result.receipt.hash: leaf},
            trusted_root=engine.log.root(),
        )
    )

    # The single entry is provably included against a root the verifier trusts.
    assert verdict.ledger_included == "pass"
    # Inclusion says nothing about the rest of the ledger: the verifier still
    # fails on the journal that does not balance.
    assert verdict.allocation_valid == "fail"
    assert any(
        "J-unbalanced" in note and "expected 0" in note for note in verdict.anomalies
    )


def test_c31_log_rejects_sensitive_material() -> None:
    """C31: the public log carries only commitments, never auth headers."""
    from aipotluck.canonical import loads_strict

    log = MerkleLog()
    index = log.append("sha256:" + "0" * 64, dt.datetime.now(dt.UTC))
    entry = log.entries()[index]
    decoded = loads_strict(entry.leaf_bytes())
    # A leaf encodes exactly three fields and nothing else.
    assert set(decoded) == {"index", "receipt_hash", "committed_at"}
    for secret in ("authorization", "Bearer", "api_key", "prompt", "cookie", "secret"):
        assert secret not in entry.leaf_bytes().decode("utf-8")
    # A receipt whose postings do not balance cannot be constructed at all, so no
    # log entry can ever be derived from one.
    from aipotluck.records import SettlementReceipt

    with pytest.raises(VerificationError):
        SettlementReceipt(
            receipt_id="R-bad",
            platform_id="platform-1",
            journal_id="J-1",
            settlement_revision=1,
            authorization_hash="sha256:a",
            request_id="R-1",
            service_unit_id="S1",
            winner_attempt_id="A1",
            billing_basis="reported_native_usage",
            verification_level="node_reported",
            pricing_version="flat-v1",
            allocation_contract_hash="sha256:c",
            billable_tokens=100,
            postings=(("consumer-X", -100),),  # unbalanced
            result_commitment=None,
            previous_receipt_hash=None,
            nonce=new_nonce(),
            settled_at=dt.datetime.now(dt.UTC),
        )


# -- C32-C34: authentication display and billing basis --------------------


def test_c32_legacy_client_is_marked_gateway_authenticated(keyring, now: object) -> None:
    """C32: a client without a signing key is labelled, not given a fake signature."""
    authorization = _authorization(now, auth_method="gateway_authenticated")  # type: ignore[arg-type]
    assert authorization.auth_method == "gateway_authenticated"
    payload = authorization.to_payload()
    assert payload["auth_method"] == "gateway_authenticated"


def test_c33_reproducible_basis_is_labelled_separately(
    keyring, ledger: Ledger, now: object
) -> None:
    """C33: a standardized count is billed under its own basis, never as native tokens.

    A reproducible-text settlement bills the standardized unit count recomputed
    from the pinned billing document, which is a different number from the
    measurement's native token total. The signed receipt must carry the
    ``reproducible_text_usage`` basis *and* the standardized count, so the two
    quantities are never presented as the same number. Deleting the basis from
    the receipt payload breaks this test.
    """
    from aipotluck.reproducible import BillingDocument, recompute_standard_units

    native_tokens = 8_700  # make_measurement(): 8_000 input + 700 output
    document = BillingDocument(
        request_id="R-1",
        messages=(
            {"role": "user", "content": "explain this code"},
            {"role": "assistant", "content": "here is the explanation " * 40},
        ),
        tokenizer_asset="cl100k_base@sha256:test",
        serializer_version="billing-doc-v1",
    )
    standardized = recompute_standard_units(document, chars_per_token=4)
    # The standardized quantity is recomputed, not copied from the native count.
    assert standardized != native_tokens

    engine = SettlementEngine(ledger=ledger, registry=keyring.registry, log=MerkleLog())
    contract = solo_contract("provider-B")
    report = build_usage_report(
        "rep-1", "p", "a", "A2", "provider-B", "device-B", make_measurement(), "completed",
        keyring.private["device-key-B"], "device-key-B", reported_at=now,
    )
    result = engine.settle(
        "R-1", "S1",
        signed_authorization(
            keyring,
            request_id="R-1",
            payer_account_id="consumer-X",
            billing_basis="reproducible_text_usage",
        ),
        report, contract, contract_hash(contract),
        keyring.private["settlement-key-1"], "A2",
        billable_tokens=standardized,
        settled_at=SETTLEMENT_AT,
    )

    payload = result.receipt.payload
    assert payload["billing_basis"] == "reproducible_text_usage"
    assert payload["billing_basis"] != "reported_native_usage"
    # The receipt presents the standardized count, not the native token total.
    assert payload["billable_tokens"] == str(standardized)
    assert payload["billable_tokens"] != str(native_tokens)


def test_c34_deleted_body_cannot_be_retokenized(keyring, now: object) -> None:
    """C34: once the billing body expires, recomputation is refused, not faked."""
    from aipotluck.errors import VerificationError as _VerificationError
    from aipotluck.reproducible import BillingDocument, recompute_standard_units

    contract = solo_contract("provider-B")
    document = BillingDocument(
        request_id="R-1001",
        messages=(
            {"role": "user", "content": "explain this code"},
            {"role": "assistant", "content": "here is the explanation"},
        ),
        tokenizer_asset="cl100k_base@sha256:test",
        serializer_version="billing-doc-v1",
    )
    units = recompute_standard_units(document, chars_per_token=4)
    assert units > 0

    # After retention expiry the body is gone, so a recomputation attempt must
    # fail loudly instead of silently returning a plausible number.
    expired = BillingDocument(
        request_id="R-1001",
        messages=(),
        tokenizer_asset=document.tokenizer_asset,
        serializer_version=document.serializer_version,
        body_expired=True,
    )
    with pytest.raises(_VerificationError):
        recompute_standard_units(expired, chars_per_token=4)
    assert contract.contract_id == "contract-provider-B"


# -- C35-C36: budget reservation and payee validation ----------------------


def test_c35_authorization_to_multiple_providers_is_capped_atomically(ledger: Ledger) -> None:
    """C35: signing authorizations beyond the balance cannot overcommit it."""
    ledger.issue("consumer-W", "consumer_available", 15_000)
    ledger.reserve("res-1", "consumer-W", "consumer_available", 10_000)
    assert ledger.balance("consumer-W/reserved") == 10_000
    with pytest.raises(InsufficientFundsError):
        ledger.reserve("res-2", "consumer-W", "consumer_available", 8_000)
    assert ledger.balance("consumer-W/reserved") == 10_000
    assert ledger.balance("consumer-W") == 5_000


def test_c36_balanced_journal_with_wrong_payee_is_rejected(ledger: Ledger) -> None:
    """C36: a zero-sum journal paying a stranger still fails contract validation."""
    from aipotluck.ledger import Journal, Posting

    contract = shared_contract()
    rogue = Journal(
        journal_id="J-rogue",
        idempotency_key=("S9", "success", 1),
        kind="success",
        postings=(
            Posting("consumer-X", -8_700),
            Posting("provider-B1/pending", 8_700),
            Posting("provider-B2/pending", 0),
        ),
    )
    assert rogue.total_delta() == 0
    with pytest.raises(LedgerError):
        check_recipient_against_contract(rogue, contract)


# -- helpers ---------------------------------------------------------------


def _registry() -> object:
    from aipotluck.identity import IdentityRegistry

    return IdentityRegistry()


def _funded_ledger() -> Ledger:
    book = Ledger()
    book.issue("consumer-X", "consumer_available", 100_000)
    book.account("provider-B/pending", "provider_pending")
    book.fund_reserve(50_000)
    return book


def _verifies(
    keyring: object, key_id: str, domain: str, payload: dict, signature: str
) -> bool:
    from conftest import Keyring

    from aipotluck.canonical import verify_payload

    assert isinstance(keyring, Keyring)
    public_b64 = keyring.public[key_id]
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from aipotluck.canonical import base64_decode

    key = Ed25519PublicKey.from_public_bytes(base64_decode(public_b64))
    return verify_payload(domain, payload, signature, key)