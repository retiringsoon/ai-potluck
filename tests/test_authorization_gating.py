"""Settlement must enforce the terms the consumer actually signed.

`SettlementEngine.settle` used to accept a bare `authorization_hash` string, so
it bound a digest it never verified. Every check here is one that a settlement
system has to make before moving credit: is this authorization real, still
valid, for this request, within budget, and strong enough on evidence?
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import (
    SETTLEMENT_AT,
    Keyring,
    contract_hash,
    signed_authorization,
)

from aipotluck.allocation import AllocationContract
from aipotluck.errors import KeyRevokedError, SettlementError
from aipotluck.identity import IdentityRegistry
from aipotluck.ledger import Ledger
from aipotluck.metering import Measurement, NormalizedUsage
from aipotluck.records import UsageReport, verify_chain
from aipotluck.settlement import SettlementEngine
from aipotluck.transparency import MerkleLog


def _contract() -> AllocationContract:
    return AllocationContract(contract_id="c", version=1, weights={"provider-B": 100})


def _engine(keyring: Keyring, ledger: Ledger) -> SettlementEngine:
    return SettlementEngine(ledger=ledger, registry=keyring.registry, log=MerkleLog())


def _report(
    keyring: Keyring,
    *,
    input_total: int = 800,
    output_total: int = 200,
    evidence_level: str = "node_reported",
    attempt_id: str = "A2",
) -> object:
    measurement = Measurement(
        measurement_id="m-1",
        scope="upstream_call",
        semantics="terminal_total",
        counter_epoch="epoch_1",
        revision=1,
        usage=NormalizedUsage(
            input_total=input_total,
            output_total=output_total,
            verification_level=evidence_level,  # type: ignore[arg-type]
        ),
        attempt_id=attempt_id,
    )
    return UsageReport(
        report_id="rep-1",
        permit_id="permit-1",
        permit_hash="permit-hash",
        acceptance_hash="acceptance-hash",
        attempt_id=attempt_id,
        provider_id="provider-B",
        device_id="device-B",
        measurement=measurement,
        result_commitment=None,
        final_event_seq=1,
        outcome="completed",
        reported_at=SETTLEMENT_AT,
        key_id="device-key-B",
    ).sign(keyring.private["device-key-B"], "device-key-B")


def _settle(
    engine: SettlementEngine,
    keyring: Keyring,
    *,
    authorization: object,
    report: object,
    **kwargs: object,
) -> object:
    return engine.settle(
        request_id="R-1001",
        service_unit_id="S-1",
        authorization=authorization,
        report=report,
        contract=_contract(),
        contract_hash=contract_hash(_contract()),
        settlement_private_key=keyring.private["settlement-key-1"],
        winner_attempt_id="A2",
        settled_at=SETTLEMENT_AT,
        **kwargs,  # type: ignore[arg-type]
    )


def test_expired_authorization_is_refused(ledger: Ledger, keyring: Keyring) -> None:
    """A window that has closed cannot authorize a charge."""
    engine = _engine(keyring, ledger)
    expired = signed_authorization(
        keyring,
        request_id="R-1001",
        issued_at=SETTLEMENT_AT - dt.timedelta(hours=2),
        expires_at=SETTLEMENT_AT - dt.timedelta(hours=1),
    )
    with pytest.raises(SettlementError, match="expired"):
        _settle(engine, keyring, authorization=expired, report=_report(keyring))
    assert ledger.balance("consumer-X") == 100_000
    assert ledger.balance("provider-B/pending") == 0


def test_not_yet_valid_authorization_is_refused(ledger: Ledger, keyring: Keyring) -> None:
    """An authorization whose window opens in the future cannot be used yet."""
    engine = _engine(keyring, ledger)
    future = signed_authorization(
        keyring,
        request_id="R-1001",
        issued_at=SETTLEMENT_AT + dt.timedelta(hours=1),
        expires_at=SETTLEMENT_AT + dt.timedelta(hours=2),
    )
    with pytest.raises(SettlementError, match="not yet valid"):
        _settle(engine, keyring, authorization=future, report=_report(keyring))
    assert ledger.balance("consumer-X") == 100_000


def test_charge_above_the_authorized_budget_is_refused(
    ledger: Ledger, keyring: Keyring
) -> None:
    """The consumer's budget cap is a ceiling, not a suggestion."""
    engine = _engine(keyring, ledger)
    authorization = signed_authorization(
        keyring, request_id="R-1001", max_charge_milli_credits=500
    )
    with pytest.raises(SettlementError, match="exceeds the 500"):
        _settle(engine, keyring, authorization=authorization, report=_report(keyring))
    assert ledger.balance("consumer-X") == 100_000
    assert ledger.balance("provider-B/pending") == 0


def test_report_below_the_evidence_policy_is_refused(
    ledger: Ledger, keyring: Keyring
) -> None:
    """A consumer who demanded upstream proof does not pay for a node report."""
    engine = _engine(keyring, ledger)
    authorization = signed_authorization(
        keyring, request_id="R-1001", minimum_evidence_level="upstream_proven"
    )
    with pytest.raises(SettlementError, match="does not meet"):
        _settle(
            engine,
            keyring,
            authorization=authorization,
            report=_report(keyring, evidence_level="node_reported"),
        )
    assert ledger.balance("consumer-X") == 100_000


def test_report_meeting_the_evidence_policy_settles(
    ledger: Ledger, keyring: Keyring
) -> None:
    """The same policy is satisfied when the report carries enough evidence."""
    engine = _engine(keyring, ledger)
    authorization = signed_authorization(
        keyring, request_id="R-1001", minimum_evidence_level="node_reported"
    )
    result = _settle(engine, keyring, authorization=authorization, report=_report(keyring))
    assert ledger.balance("consumer-X") == 99_000
    assert ledger.balance("provider-B/pending") == 1_000
    assert result.receipt.payload["billing_basis"] == "reported_native_usage"


def test_authorization_for_another_request_is_refused(
    ledger: Ledger, keyring: Keyring
) -> None:
    """An authorization is bound to one request and cannot be reused for another."""
    engine = _engine(keyring, ledger)
    other = signed_authorization(keyring, request_id="R-OTHER")
    with pytest.raises(SettlementError, match="covers request"):
        _settle(engine, keyring, authorization=other, report=_report(keyring))
    assert ledger.balance("consumer-X") == 100_000


def test_unsigned_authorization_is_refused(ledger: Ledger, keyring: Keyring) -> None:
    """A structurally valid but unregistered-key authorization does not settle."""
    from aipotluck.identity import generate_key_pair, load_private_key
    from aipotluck.records import SpendAuthorization, new_nonce

    engine = _engine(keyring, ledger)
    _, rogue_private_b64 = generate_key_pair()
    rogue = load_private_key(rogue_private_b64)
    authorization = SpendAuthorization(
        authorization_id="auth-rogue",
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
        issued_at=SETTLEMENT_AT - dt.timedelta(minutes=1),
        expires_at=SETTLEMENT_AT + dt.timedelta(minutes=5),
        key_id="rogue-key",
    ).sign(rogue, "rogue-key")

    with pytest.raises(SettlementError, match="failed verification"):
        _settle(engine, keyring, authorization=authorization, report=_report(keyring))
    assert ledger.balance("consumer-X") == 100_000


def test_cross_account_charge_without_a_grant_is_refused(
    ledger: Ledger, keyring: Keyring
) -> None:
    """A signature proves who signed, not whose money may be spent.

    This is the regression for a real hole: an earlier version of this module
    accepted any payer string the signer wrote, so X's valid consumer key could
    name Y's funded account as its payer and drain it without Y ever signing.
    """
    engine = _engine(keyring, ledger)
    funded_y = ledger.balance("consumer-Y")
    authorization = signed_authorization(
        keyring,
        request_id="R-1001",
        payer_account_id="consumer-Y",
        consumer_id="consumer-X",
    )
    with pytest.raises(SettlementError, match="no authority to spend"):
        _settle(engine, keyring, authorization=authorization, report=_report(keyring))
    assert ledger.balance("consumer-Y") == funded_y
    assert ledger.balance("consumer-X") == 100_000
    assert ledger.balance("provider-B/pending") == 0


def test_cross_account_charge_is_allowed_with_a_grant(
    ledger: Ledger, keyring: Keyring
) -> None:
    """A team member may spend a team wallet when the wallet granted that right."""
    engine = _engine(keyring, ledger)
    funded_y = ledger.balance("consumer-Y")
    keyring.registry.grant_spend_authority(
        payer_account_id="consumer-Y",
        spender_id="consumer-X",
        granted_at=SETTLEMENT_AT - dt.timedelta(hours=1),
        max_milli_credits=20_000,
    )
    authorization = signed_authorization(
        keyring,
        request_id="R-1001",
        payer_account_id="consumer-Y",
        consumer_id="consumer-X",
    )
    _settle(engine, keyring, authorization=authorization, report=_report(keyring))
    assert ledger.balance("consumer-Y") == funded_y - 1_000
    assert ledger.balance("consumer-X") == 100_000


def test_grant_cap_bounds_what_may_be_authorized(
    ledger: Ledger, keyring: Keyring
) -> None:
    """A grant caps the authorization it backs, not merely the final bill.

    The broad-cap authorization is refused even though the usage would come in
    under the grant: permitting the signature would let its holder commit the
    account to more than they were granted.
    """
    engine = _engine(keyring, ledger)
    keyring.registry.grant_spend_authority(
        payer_account_id="consumer-Y",
        spender_id="consumer-X",
        granted_at=SETTLEMENT_AT - dt.timedelta(hours=1),
        max_milli_credits=5_000,
    )
    broad = signed_authorization(
        keyring,
        request_id="R-1001",
        payer_account_id="consumer-Y",
        consumer_id="consumer-X",
        max_charge_milli_credits=20_000,
    )
    with pytest.raises(SettlementError, match="caps a charge"):
        _settle(engine, keyring, authorization=broad, report=_report(keyring))
    assert ledger.balance("consumer-Y") == 100_000

    # The same grant accepts an authorization that stays inside it.
    narrow = signed_authorization(
        keyring,
        request_id="R-1002",
        payer_account_id="consumer-Y",
        consumer_id="consumer-X",
        max_charge_milli_credits=5_000,
    )
    engine.settle(
        request_id="R-1002",
        service_unit_id="S-2",
        authorization=narrow,
        report=_report(keyring),
        contract=_contract(),
        contract_hash=contract_hash(_contract()),
        settlement_private_key=keyring.private["settlement-key-1"],
        winner_attempt_id="A2",
        settled_at=SETTLEMENT_AT,
    )
    assert ledger.balance("consumer-Y") == 99_000


def test_revoked_grant_stops_further_charges(ledger: Ledger, keyring: Keyring) -> None:
    """Withdrawing the grant ends the cross-account permission."""
    engine = _engine(keyring, ledger)
    keyring.registry.grant_spend_authority(
        payer_account_id="consumer-Y",
        spender_id="consumer-X",
        granted_at=SETTLEMENT_AT - dt.timedelta(hours=1),
    )
    keyring.registry.revoke_spend_authority(
        "consumer-Y", "consumer-X", SETTLEMENT_AT - dt.timedelta(minutes=30)
    )
    authorization = signed_authorization(
        keyring,
        request_id="R-1001",
        payer_account_id="consumer-Y",
        consumer_id="consumer-X",
    )
    with pytest.raises(SettlementError, match="not in force"):
        _settle(engine, keyring, authorization=authorization, report=_report(keyring))
    assert ledger.balance("consumer-Y") == 100_000


def test_self_spend_needs_no_grant(ledger: Ledger, keyring: Keyring) -> None:
    """An identity may always draw on its own account."""
    engine = _engine(keyring, ledger)
    authorization = signed_authorization(
        keyring, request_id="R-1001", payer_account_id="consumer-X", consumer_id="consumer-X"
    )
    _settle(engine, keyring, authorization=authorization, report=_report(keyring))
    assert ledger.balance("consumer-X") == 99_000


def test_downward_revision_posts_only_the_difference(
    ledger: Ledger, keyring: Keyring
) -> None:
    """A corrected total of 800 after 1,000 books a 200 refund, not a re-charge."""
    engine = _engine(keyring, ledger)
    authorization = signed_authorization(keyring, request_id="R-1001")
    supply_before = ledger.total_supply()
    first = _settle(
        engine, keyring, authorization=authorization, report=_report(keyring)
    )
    assert first.receipt.payload["billable_tokens"] == "1000"
    assert ledger.balance("consumer-X") == 99_000
    assert ledger.balance("provider-B/pending") == 1_000

    second = _settle(
        engine,
        keyring,
        authorization=authorization,
        report=_report(keyring, input_total=640, output_total=160),
        settlement_revision=2,
        already_recorded={"provider-B": 1_000},
    )
    # Only the 200 difference moves: a correction is not a second full charge.
    assert ledger.balance("consumer-X") == 99_200
    assert ledger.balance("provider-B/pending") == 800
    assert ledger.total_supply() == supply_before
    assert second.receipt.payload["settlement_revision"] == "2"


def test_receipt_binds_the_verified_authorization_hash(
    ledger: Ledger, keyring: Keyring
) -> None:
    """The receipt references the authorization record that was actually checked."""
    engine = _engine(keyring, ledger)
    authorization = signed_authorization(keyring, request_id="R-1001")
    result = _settle(engine, keyring, authorization=authorization, report=_report(keyring))
    assert result.receipt.payload["authorization_hash"] == authorization.hash

    # And the authorization itself verifies against the registry as a consumer record.
    verify_chain(keyring.registry, authorization, "consumer")


def test_gateway_authenticated_authorization_is_labelled_not_forged(
    ledger: Ledger, keyring: Keyring
) -> None:
    """A gateway-signed authorization settles but keeps its honest label.

    A legacy client with no signing key is authenticated by the platform token
    instead. It must be accepted, and the receipt must not imply a consumer
    signature it never had.
    """
    engine = _engine(keyring, ledger)
    # The gateway holds its own registered key under the platform settlement role.
    keyring.registry.register_key(
        "gateway-key-1",
        "platform-1",
        "platform_settlement",
        keyring.public["settlement-key-1"],
        dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
    )
    keyring.private["gateway-key-1"] = keyring.private["settlement-key-1"]
    authorization = signed_authorization(
        keyring,
        request_id="R-1001",
        auth_method="gateway_authenticated",
        key_id="gateway-key-1",
    )
    result = _settle(engine, keyring, authorization=authorization, report=_report(keyring))
    assert result.receipt.payload["authorization_hash"] == authorization.hash
    assert authorization.payload["auth_method"] == "gateway_authenticated"


def test_evidence_policy_rejects_an_unknown_level(
    ledger: Ledger, keyring: Keyring
) -> None:
    """An unrecognised evidence level fails the policy instead of passing it."""
    engine = _engine(keyring, ledger)
    authorization = signed_authorization(keyring, request_id="R-1001")
    with pytest.raises(SettlementError, match="cannot evaluate evidence policy"):
        _settle(
            engine,
            keyring,
            authorization=authorization,
            report=_report(keyring, evidence_level="not_a_real_level"),
        )
    assert ledger.balance("consumer-X") == 100_000


def test_unrelated_registry_cannot_settle_with_foreign_authorization(
    ledger: Ledger, keyring: Keyring
) -> None:
    """An authorization from another platform's registry must not be accepted."""
    engine = SettlementEngine(
        ledger=ledger, registry=IdentityRegistry(), log=MerkleLog()
    )
    authorization = signed_authorization(keyring, request_id="R-1001")
    with pytest.raises(SettlementError, match="failed verification"):
        _settle(engine, keyring, authorization=authorization, report=_report(keyring))
    assert ledger.balance("consumer-X") == 100_000

# -- admission time vs self-reported time ----------------------------------


def test_backdated_report_cannot_escape_a_revocation(
    ledger: Ledger, keyring: Keyring
) -> None:
    """A revoked key cannot make a report acceptable by backdating it.

    Admission must judge key validity at the instant the platform accepted the
    message. Judging it at the record's self-reported time lets whoever holds a
    revoked key sign a report claiming any earlier date and have it accepted.
    """
    import datetime as _dt

    from aipotluck.records import UsageReport, verify_chain

    revocation = SETTLEMENT_AT - _dt.timedelta(minutes=1)
    keyring.registry.revoke_key("device-key-B", revocation)

    measurement = Measurement(
        measurement_id="m-backdated",
        scope="upstream_call",
        semantics="terminal_total",
        counter_epoch="epoch_1",
        revision=1,
        usage=NormalizedUsage(input_total=800, output_total=200),
        attempt_id="A2",
    )
    # Signed after the revocation, but claiming it was reported an hour earlier.
    backdated = UsageReport(
        report_id="rep-backdated",
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
        reported_at=SETTLEMENT_AT - _dt.timedelta(hours=1),
        key_id="device-key-B",
    ).sign(keyring.private["device-key-B"], "device-key-B")

    # Historic verification of an already-accepted record uses its own timestamp.
    verify_chain(
        keyring.registry, backdated, "provider_device", owner_id="device-B"
    )

    # Admission uses the witness instant, where the key is already revoked.
    with pytest.raises(KeyRevokedError):
        verify_chain(
            keyring.registry,
            backdated,
            "provider_device",
            owner_id="device-B",
            witnessed_at=SETTLEMENT_AT,
        )

    # And settlement is an admission decision, so it refuses outright.
    authorization = signed_authorization(keyring, request_id="R-1001")
    with pytest.raises(SettlementError):
        _settle(engine=_engine(keyring, ledger), keyring=keyring,
                authorization=authorization, report=backdated)
    assert ledger.balance("consumer-X") == 100_000
    assert ledger.balance("provider-B/pending") == 0
