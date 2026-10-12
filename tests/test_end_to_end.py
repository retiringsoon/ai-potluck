"""End-to-end closed loop and offline verification.

Walks the design's worked example (section 14.1 and 23.5) through the real code
path: consumer authorization, dispatch, contributor measurement, settlement,
signed receipt, transparency log, and a party's independent offline check.

The end-to-end assertions check accounting conservation and the failure
attribution rules. They do not exercise real upstreams, network failure, or
database concurrency, and are not a production-readiness claim.
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import (
    SETTLEMENT_AT,
    contract_hash,
    make_measurement,
    shared_contract,
    solo_contract,
)

from aipotluck.canonical import sign_payload
from aipotluck.coordinator import Coordinator
from aipotluck.errors import VerificationError
from aipotluck.ledger import Ledger
from aipotluck.records import (
    DOMAIN_AUTHORIZATION,
    DispatchPermit,
    ProviderAcceptance,
    SignedRecord,
    SpendAuthorization,
    new_nonce,
)
from aipotluck.settlement import SettlementEngine, build_usage_report
from aipotluck.transparency import CheckpointHistory, MerkleLog, verify_inclusion
from aipotluck.verify import VerificationInput, verify_offline


def _authorization(keyring, now: dt.datetime) -> SpendAuthorization:
    """Build and sign a consumer authorization."""
    return SpendAuthorization(
        authorization_id="auth-R1",
        payer_account_id="consumer-X",
        consumer_id="consumer-X",
        request_id="R-1001",
        request_digest="sha256:payload",
        max_charge_milli_credits=20_000,
        pricing_version="flat-v1",
        billing_basis="reported_native_usage",
        minimum_evidence_level="node_reported",
        allowed_models=frozenset({"coding-pool"}),
        allowed_trust_pools=frozenset({"trusted-beta"}),
        max_attempts=2,
        duplicate_execution_policy="bounded_model_only",
        max_provider_fanout=2,
        nonce=new_nonce(),
        issued_at=now,
        expires_at=now + dt.timedelta(minutes=5),
        key_id="consumer-key-1",
    ).sign(keyring.private["consumer-key-1"], "consumer-key-1")


def _platform_authorization(keyring, now: dt.datetime) -> object:
    """Build the authorization a center mints for a request it admitted itself.

    This is the protocol's second authentication method, ``gateway_authenticated``:
    the platform's own settlement key signs the terms it admitted, because the
    requester authenticated with a credential the center holds — an
    operator-issued API key — rather than with a signature the requester
    produced. The authorization is therefore real evidence of a *different*
    strength, and the label is what keeps the two apart: settlement and
    verification both read it to decide which key had to sign.
    """
    return SpendAuthorization(
        authorization_id="auth-R1",
        payer_account_id="consumer-X",
        consumer_id="consumer-X",
        request_id="R-1001",
        request_digest="sha256:payload",
        max_charge_milli_credits=20_000,
        pricing_version="flat-v1",
        billing_basis="reported_native_usage",
        minimum_evidence_level="node_reported",
        allowed_models=frozenset({"coding-pool"}),
        allowed_trust_pools=frozenset({"trusted-beta"}),
        max_attempts=2,
        duplicate_execution_policy="bounded_model_only",
        max_provider_fanout=2,
        nonce=new_nonce(),
        issued_at=now,
        expires_at=now + dt.timedelta(minutes=5),
        auth_method="gateway_authenticated",
        key_id="settlement-key-1",
    ).sign(keyring.private["settlement-key-1"], "settlement-key-1")


def _permit(keyring, now: dt.datetime, authorization_hash: str, epoch: int = 1) -> object:
    """Build and sign a dispatch permit."""
    return DispatchPermit(
        permit_id="permit-A1",
        authorization_id="auth-R1",
        authorization_hash=authorization_hash,
        attempt_id="A1",
        dispatch_epoch=epoch,
        resource_id="res-B",
        provider_id="provider-B",
        allocation_contract_hash="sha256:contract",
        max_exposed_milli_credits=20_000,
        request_digest="sha256:payload",
        policy_version=7,
        issued_at=now,
        lease_expires_at=now + dt.timedelta(seconds=20),
        platform_id="platform-1",
        key_id="settlement-key-1",
    ).sign(keyring.private["settlement-key-1"], "settlement-key-1")


def _acceptance(keyring, now: dt.datetime, permit_hash: str) -> object:
    """Build and sign a contributor acceptance."""
    return ProviderAcceptance(
        acceptance_id="acc-A1",
        permit_id="permit-A1",
        permit_hash=permit_hash,
        attempt_id="A1",
        provider_id="provider-B",
        device_id="device-B",
        local_policy_version=3,
        local_reserved_milli_credits=20_000,
        accepted_at=now,
    ).sign(keyring.private["device-key-B"], "device-key-B")


def test_full_loop_from_authorization_to_settlement(
    keyring, ledger: Ledger, now: dt.datetime
) -> None:
    """The whole chain verifies and conserves credit end to end."""
    authorization = _authorization(keyring, now)
    permit = _permit(keyring, now, authorization.hash)
    acceptance = _acceptance(keyring, now, permit.hash)

    engine = SettlementEngine(ledger=ledger, registry=keyring.registry, log=MerkleLog())
    contract = solo_contract("provider-B")

    report = build_usage_report(
        "rep-A1", permit.hash, acceptance.hash, "A1", "provider-B", "device-B",
        make_measurement(attempt_id="A1"), "completed",
        keyring.private["device-key-B"], "device-key-B", reported_at=now,
    )
    result = engine.settle(
        request_id="R-1001",
        service_unit_id="R-1001",
        authorization=authorization,
        report=report,
        contract=contract,
        contract_hash=contract_hash(contract),
        settlement_private_key=keyring.private["settlement-key-1"],
        winner_attempt_id="A1",
        settled_at=SETTLEMENT_AT,
    )

    # Accounting: the consumer paid 8,700 and the contributor earned it.
    assert ledger.balance("consumer-X") == 100_000 - 8_700
    assert ledger.balance("provider-B/pending") == 8_700
    assert sum(result.allocations.values()) == 8_700

    # Every link in the chain verifies against the trusted registry.
    from aipotluck.records import verify_chain

    verify_chain(keyring.registry, authorization, "consumer")
    verify_chain(keyring.registry, permit, "platform_settlement")
    verify_chain(keyring.registry, acceptance, "provider_device")
    verify_chain(keyring.registry, report, "provider_device", owner_id="device-B")
    verify_chain(keyring.registry, result.receipt, "platform_settlement")

    # The receipt is included in the transparency log.
    entry = engine.log_entries()[result.log_index]
    assert verify_inclusion(result.inclusion_proof, entry.leaf_bytes())


def test_worked_example_failure_attribution(keyring, ledger: Ledger, now: dt.datetime) -> None:
    """Section 14.1: A fails, B succeeds; only B is billed, A's cost is recorded."""
    coordinator = Coordinator()
    aggregate = coordinator.create_request("R-1001", "tenant-1", "idem-1", "sha256:payload")
    coordinator.open_attempt("R-1001", "A1", "res-A", "provider-A")
    coordinator.open_attempt("R-1001", "A2", "res-B", "provider-B")
    coordinator.commit_output("R-1001", "A2", 1, "gen-A2")

    # A consumed 8,300 tokens before failing; B consumed 8,700 and succeeded.
    coordinator.record_usage("R-1001", "A1", 8_300)
    coordinator.record_usage("R-1001", "A2", 8_700)

    view = aggregate.status_view()
    assert view["usage"]["all_attempts_known_tokens"] == 17_000
    assert view["usage"]["selected_service_tokens"] == 8_700
    assert view["selected_attempt_id"] == "A2"

    # The consumer pays for B only; A's consumption stays a resource record.
    contract = solo_contract("provider-B")
    ledger.settle_success("R-1001", "consumer-X", ["provider-B"], 8_700, contract, "J-1")
    assert ledger.balance("consumer-X") == 100_000 - 8_700
    assert ledger.balance("provider-B/pending") == 8_700
    assert ledger.balance("provider-A/pending") == 0


def test_unknown_usage_is_shown_as_unknown(keyring, ledger: Ledger) -> None:
    """Section 14.2: unknown usage is reported as pending, never as zero."""
    coordinator = Coordinator()
    aggregate = coordinator.create_request("R-1002", "tenant-1", "idem-2", "sha256:p2")
    coordinator.open_attempt("R-1002", "A1", "res-A", "provider-A")
    coordinator.open_attempt("R-1002", "A2", "res-B", "provider-B")
    coordinator.commit_output("R-1002", "A2", 1, "gen-A2")
    coordinator.record_usage("R-1002", "A2", 8_700)
    coordinator.mark_usage_unknown("R-1002", "A1")

    view = aggregate.status_view()
    assert view["usage"]["all_attempts_known_tokens"] == 8_700
    assert view["usage"]["unknown_attempts"] == 1
    assert aggregate.usage_status == "PARTIAL"


def test_multi_party_example_totals_conserve(ledger: Ledger) -> None:
    """Section 23.5: X spends 10, Y spends 10; A earns 9, B 4, C 7."""
    ledger.settle_success(
        "S1", "consumer-X", ["provider-A"], 6_000, solo_contract("provider-A"), "J-1"
    )
    ledger.settle_success(
        "S2", "consumer-X", ["provider-B"], 4_000, solo_contract("provider-B"), "J-2"
    )
    ledger.settle_success(
        "S3", "consumer-Y", ["provider-A"], 3_000, solo_contract("provider-A"), "J-3"
    )
    ledger.settle_success(
        "S4", "consumer-Y", ["provider-C"], 7_000, solo_contract("provider-C"), "J-4"
    )

    assert ledger.balance("consumer-X") == 90_000
    assert ledger.balance("consumer-Y") == 90_000
    assert ledger.balance("provider-A/pending") == 9_000
    assert ledger.balance("provider-B/pending") == 4_000
    assert ledger.balance("provider-C/pending") == 7_000
    total_spend = 20_000
    total_earned = 9_000 + 4_000 + 7_000
    assert total_spend == total_earned


def test_shared_resource_split(ledger: Ledger) -> None:
    """Section 23.7: 8,700 at 70/30 splits into 6,090 and 2,610."""
    contract = shared_contract()
    _, allocations = ledger.settle_success(
        "S1", "consumer-X", ["provider-B1", "provider-B2"], 8_700, contract, "J-1"
    )
    assert allocations == {"provider-B1": 6_090, "provider-B2": 2_610}
    assert ledger.balance("provider-B1/pending") == 6_090
    assert ledger.balance("provider-B2/pending") == 2_610
    assert ledger.balance("consumer-X") == 91_300


# -- offline verification ---------------------------------------------------


def test_offline_verification_passes_on_honest_records(
    keyring, ledger: Ledger, now: dt.datetime
) -> None:
    """A party's own records verify with every dimension separated."""
    authorization = _authorization(keyring, now)
    permit = _permit(keyring, now, authorization.hash)
    acceptance = _acceptance(keyring, now, permit.hash)
    engine = SettlementEngine(ledger=ledger, registry=keyring.registry, log=MerkleLog())
    contract = solo_contract("provider-B")
    report = build_usage_report(
        "rep-A1", permit.hash, acceptance.hash, "A1", "provider-B", "device-B",
        make_measurement(attempt_id="A1"), "completed",
        keyring.private["device-key-B"], "device-key-B", reported_at=now,
    )
    result = engine.settle(
        "R-1001", "S1", authorization, report, contract,
        contract_hash(contract), keyring.private["settlement-key-1"], "A1",
        settled_at=SETTLEMENT_AT,
    )
    entry = engine.log_entries()[result.log_index]

    outcome = verify_offline(
        VerificationInput(
            registry=keyring.registry,
            authorizations=[authorization],
            permits=[permit],
            acceptances=[acceptance],
            usage_reports=[report],
            receipts=[result.receipt],
            journals=[result.journal],
            contracts={contract_hash(contract): contract},
            inclusion_proofs={result.receipt.hash: result.inclusion_proof},
            leaf_bytes={result.receipt.hash: entry.leaf_bytes()},
            # Presence in the log is only meaningful against a root the party
            # already trusts, so the verifier needs the checkpointed root here.
            trusted_root=engine.log.root(),
            expected_receipt_ids=[result.receipt.record_id],
            expected_sequence=[1],
            observed_sequence=[1],
        )
    )
    assert outcome.signature_valid == "pass"
    assert outcome.allocation_valid == "pass"
    assert outcome.ledger_included == "pass"
    assert outcome.account_history_complete == "pass"
    assert outcome.recomputed_earnings == {"provider-B": 8_700}
    assert outcome.recomputed_spend == {"consumer-X": 8_700}


def test_platform_authorized_receipt_verifies_under_its_own_method(
    keyring, ledger: Ledger, now: dt.datetime
) -> None:
    """A receipt behind a gateway-authenticated authorization verifies honestly.

    The center admits an API-key request on a credential it holds, so the
    authorization it stores is signed by the platform rather than by the
    consumer. Verification follows the record's own `auth_method` instead of
    assuming one, which is what lets an operator audit such a receipt and get a
    true verdict: reporting `fail` here would be a false negative against the
    platform's own promise that every charge is provable.
    """
    authorization = _platform_authorization(keyring, now)
    permit = _permit(keyring, now, authorization.hash)
    acceptance = _acceptance(keyring, now, permit.hash)
    engine = SettlementEngine(ledger=ledger, registry=keyring.registry, log=MerkleLog())
    contract = solo_contract("provider-B")
    report = build_usage_report(
        "rep-A1", permit.hash, acceptance.hash, "A1", "provider-B", "device-B",
        make_measurement(attempt_id="A1"), "completed",
        keyring.private["device-key-B"], "device-key-B", reported_at=now,
    )
    result = engine.settle(
        "R-1001", "S1", authorization, report, contract,
        contract_hash(contract), keyring.private["settlement-key-1"], "A1",
        settled_at=SETTLEMENT_AT,
    )
    entry = engine.log_entries()[result.log_index]

    outcome = verify_offline(
        VerificationInput(
            registry=keyring.registry,
            authorizations=[authorization],
            permits=[permit],
            acceptances=[acceptance],
            usage_reports=[report],
            receipts=[result.receipt],
            journals=[result.journal],
            contracts={contract_hash(contract): contract},
            inclusion_proofs={result.receipt.hash: result.inclusion_proof},
            leaf_bytes={result.receipt.hash: entry.leaf_bytes()},
            trusted_root=engine.log.root(),
            expected_receipt_ids=[result.receipt.record_id],
            expected_sequence=[1],
            observed_sequence=[1],
        )
    )
    assert outcome.signature_valid == "pass"
    assert outcome.allocation_valid == "pass"
    assert outcome.ledger_included == "pass"
    assert not [item for item in outcome.anomalies if "spend-authorization" in item]
    # The label survives verification: nobody may read this as a consumer
    # signature, because the consumer never signed it.
    assert authorization.payload["auth_method"] == "gateway_authenticated"


def test_unknown_authorization_method_is_reported_not_passed(
    keyring, now: dt.datetime
) -> None:
    """A method this build does not define has no check it could honestly pass.

    The signature here is valid — the consumer key really signed this payload —
    so the failure is attributable to the label alone. Falling back to the
    consumer check for an unrecognised `auth_method` would let a method added
    later inherit a trust level nobody granted it, and would report `pass` for a
    record this build cannot interpret.
    """
    signed = _authorization(keyring, now)
    payload = {**signed.payload, "auth_method": "delegated_gateway_v2"}
    record = SignedRecord(
        schema=signed.schema,
        record_id=signed.record_id,
        revision=signed.revision,
        signed_at=signed.signed_at,
        signer_key_id=signed.signer_key_id,
        signer_id=signed.signer_id,
        signature=sign_payload(
            DOMAIN_AUTHORIZATION, payload, keyring.private["consumer-key-1"]
        ),
        payload=payload,
    )

    outcome = verify_offline(
        VerificationInput(registry=keyring.registry, authorizations=[record])
    )

    assert outcome.signature_valid == "fail"
    assert any("delegated_gateway_v2" in anomaly for anomaly in outcome.anomalies)


def test_offline_verification_flags_wrong_payee(
    keyring, ledger: Ledger, now: dt.datetime
) -> None:
    """Recomputing the allocation catches a receipt that pays a stranger."""
    from aipotluck.records import SettlementReceipt

    authorization = _authorization(keyring, now)
    permit = _permit(keyring, now, authorization.hash)
    acceptance = _acceptance(keyring, now, permit.hash)
    contract = solo_contract("provider-B")

    rogue = SettlementReceipt(
        receipt_id="R-rogue",
        platform_id="platform-1",
        journal_id="J-rogue",
        settlement_revision=1,
        authorization_hash=authorization.hash,
        request_id="R-1001",
        service_unit_id="S1",
        winner_attempt_id="A1",
        billing_basis="reported_native_usage",
        verification_level="node_reported",
        pricing_version="flat-v1",
        allocation_contract_hash=contract_hash(contract),
        billable_tokens=8_700,
        postings=(("consumer-X", -8_700), ("provider-E/pending", 8_700)),
        result_commitment=None,
        previous_receipt_hash=None,
        nonce=new_nonce(),
        settled_at=now,
        key_id="settlement-key-1",
    ).sign(keyring.private["settlement-key-1"], "settlement-key-1")

    report = build_usage_report(
        "rep-A1", permit.hash, acceptance.hash, "A1", "provider-B", "device-B",
        make_measurement(attempt_id="A1"), "completed",
        keyring.private["device-key-B"], "device-key-B", reported_at=now,
    )
    outcome = verify_offline(
        VerificationInput(
            registry=keyring.registry,
            authorizations=[authorization],
            permits=[permit],
            acceptances=[acceptance],
            usage_reports=[report],
            receipts=[rogue],
            contracts={contract_hash(contract): contract},
        )
    )
    assert outcome.allocation_valid == "fail"
    assert any("requires" in anomaly for anomaly in outcome.anomalies)


def test_verification_reports_provenance_honestly(
    keyring, ledger: Ledger, now: dt.datetime
) -> None:
    """A signed node report never claims upstream attestation."""
    authorization = _authorization(keyring, now)
    permit = _permit(keyring, now, authorization.hash)
    acceptance = _acceptance(keyring, now, permit.hash)
    engine = SettlementEngine(ledger=ledger, registry=keyring.registry, log=MerkleLog())
    contract = solo_contract("provider-B")
    report = build_usage_report(
        "rep-A1", permit.hash, acceptance.hash, "A1", "provider-B", "device-B",
        make_measurement(attempt_id="A1"), "completed",
        keyring.private["device-key-B"], "device-key-B", reported_at=now,
    )
    result = engine.settle(
        "R-1001", "S1", authorization, report, contract,
        contract_hash(contract), keyring.private["settlement-key-1"], "A1",
        settled_at=SETTLEMENT_AT,
    )
    entry = engine.log_entries()[result.log_index]
    outcome = verify_offline(
        VerificationInput(
            registry=keyring.registry,
            receipts=[result.receipt],
            usage_reports=[report],
            contracts={contract_hash(contract): contract},
            inclusion_proofs={result.receipt.hash: result.inclusion_proof},
            leaf_bytes={result.receipt.hash: entry.leaf_bytes()},
        )
    )
    assert outcome.metering_provenance == "pass"
    assert outcome.evidence_levels == {"node_reported": 1}
    assert any("not that the upstream consumed them" in a for a in outcome.anomalies)


def test_policy_requiring_upstream_proof_fails_on_node_reports(
    keyring, ledger: Ledger, now: dt.datetime
) -> None:
    """When policy demands upstream proof, node reports do not satisfy it."""
    authorization = _authorization(keyring, now)
    permit = _permit(keyring, now, authorization.hash)
    acceptance = _acceptance(keyring, now, permit.hash)
    engine = SettlementEngine(ledger=ledger, registry=keyring.registry, log=MerkleLog())
    contract = solo_contract("provider-B")
    report = build_usage_report(
        "rep-A1", permit.hash, acceptance.hash, "A1", "provider-B", "device-B",
        make_measurement(attempt_id="A1"), "completed",
        keyring.private["device-key-B"], "device-key-B", reported_at=now,
    )
    result = engine.settle(
        "R-1001", "S1", authorization, report, contract,
        contract_hash(contract), keyring.private["settlement-key-1"], "A1",
        settled_at=SETTLEMENT_AT,
    )
    outcome = verify_offline(
        VerificationInput(
            registry=keyring.registry,
            receipts=[result.receipt],
            usage_reports=[report],
            contracts={contract_hash(contract): contract},
            require_upstream_proof=True,
        )
    )
    assert outcome.metering_provenance == "fail"
    assert any("requires upstream_proven" in a for a in outcome.anomalies)


def test_missing_sequence_reports_history_incomplete(
    keyring, ledger: Ledger, now: dt.datetime
) -> None:
    """A gap in the account sequence is reported rather than hidden."""
    outcome = verify_offline(
        VerificationInput(
            registry=keyring.registry,
            expected_sequence=[1, 2, 3, 4],
            observed_sequence=[1, 2, 4],
        )
    )
    assert outcome.account_history_complete == "fail"
    assert any("gap" in anomaly for anomaly in outcome.anomalies)


# -- transparency log -------------------------------------------------------


def test_inclusion_proof_survives_later_appends() -> None:
    """An earlier receipt keeps verifying as the tree grows."""
    log = MerkleLog()
    first = log.append("sha256:" + "1" * 64, dt.datetime(2026, 10, 6, tzinfo=dt.UTC))
    first_proof = log.inclusion_proof(first)
    first_leaf = log.entries()[first].leaf_bytes()
    for index in range(5):
        log.append("sha256:" + str(index) * 64, dt.datetime(2026, 10, 6, tzinfo=dt.UTC))
    assert log.size() == 6
    assert verify_inclusion(first_proof, first_leaf)


@pytest.mark.parametrize("size", [1, 2, 3, 4, 5, 7, 8, 9, 16, 17])
def test_every_leaf_verifies_across_tree_sizes(size: int) -> None:
    """Inclusion proofs stay valid on odd-sized trees, not just powers of two.

    An audit path that only works on even trees would silently fail to prove
    inclusion for real logs, whose size is whatever the receipt count happens to
    be.
    """
    log = MerkleLog()
    for index in range(size):
        log.append(
            "sha256:" + f"{index:064d}",
            dt.datetime(2026, 10, 6, tzinfo=dt.UTC),
        )
    stored = {entry.index: entry.leaf_bytes() for entry in log.entries()}
    for index in range(size):
        assert verify_inclusion(log.inclusion_proof(index), stored[index]), index


def test_checkpoint_signature_and_monotonic_epochs(keyring) -> None:
    """Checkpoints are signed and must advance monotonically."""
    from aipotluck.transparency import Checkpoint

    log = MerkleLog()
    log.append("sha256:" + "a" * 64, dt.datetime.now(dt.UTC))
    checkpoint = Checkpoint(
        platform_id="platform-1",
        tree_size=log.size(),
        root_hash=log.root(),
        checkpoint_epoch=1,
        issued_at=dt.datetime.now(dt.UTC),
        key_id="settlement-key-1",
    ).sign(keyring.private["settlement-key-1"])
    public = keyring.registry.get_key("settlement-key-1").public_key()
    assert checkpoint.verify(public)

    history = CheckpointHistory(platform_id="platform-1")
    history.observe(checkpoint)
    with pytest.raises(VerificationError):
        history.observe(checkpoint)


def test_consistency_proof_detects_a_rewritten_prefix() -> None:
    """A log that rewrote history fails the consistency proof."""
    from aipotluck.transparency import verify_checkpoint_history

    honest = MerkleLog()
    for index in range(4):
        honest.append("sha256:" + f"{index:064d}", dt.datetime(2026, 10, 6, tzinfo=dt.UTC))
    old_root = honest.root()
    honest.append("sha256:" + "9" * 64, dt.datetime(2026, 10, 6, tzinfo=dt.UTC))

    proof = honest.consistency_proof(4)
    assert proof["old_root"] == old_root

    # A cached root that does not match the proof's starting point is a fork.
    from aipotluck.transparency import Checkpoint, CheckpointHistory

    history = CheckpointHistory(platform_id="platform-1")
    base = dt.datetime.now(dt.UTC)
    history.observe(
        Checkpoint("platform-1", 4, "sha256:deadbeef", 1, base, "settlement-key-1")
    )
    history.observe(
        Checkpoint("platform-1", honest.size(), honest.root(), 2, base, "settlement-key-1")
    )
    anomalies = verify_checkpoint_history(history, {4: proof})
    assert any("cached root" in note for note in anomalies)