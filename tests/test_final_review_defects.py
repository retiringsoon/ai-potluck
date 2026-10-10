"""Regression tests for the defects found in the final review.

Each of these was reproduced with a failing script before the fix. They live in
the repository because each guards an invariant that is easy to reintroduce:
reservations must close their round trip, verification must degrade rather than
raise, a device must not redirect earnings, and device registration must work
outside the test fixture.
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import SETTLEMENT_AT, contract_hash, signed_authorization

from aipotluck.allocation import AllocationContract
from aipotluck.errors import IdentityBindingError, SettlementError
from aipotluck.identity import IdentityRegistry, generate_key_pair, load_private_key
from aipotluck.ledger import Ledger
from aipotluck.metering import Measurement, NormalizedUsage
from aipotluck.records import UsageReport
from aipotluck.settlement import SettlementEngine
from aipotluck.transparency import Checkpoint, MerkleLog
from aipotluck.verify import VerificationInput, verify_offline

#: Keys are registered before the modelled settlement instant, so they are valid
#: at admission time no matter what the wall clock says. Admission judges key
#: validity at the platform's witness instant, not the record's self-reported one.
KEY_AT = SETTLEMENT_AT - dt.timedelta(days=1)


def test_reserve_consume_settle_closes_the_round_trip() -> None:
    """Consuming a reservation must not strand the reserved credit.

    The reservation moves credit into a settlement payable. If settlement only
    ever charged the payer's spendable balance, that payable would accumulate
    forever and the reserved amount would become unreachable.
    """
    ledger = Ledger()
    ledger.issue("consumer-1", "consumer_available", 1_000)
    ledger.account("provider-A/pending", "provider_pending")
    contract = AllocationContract(contract_id="c", version=1, weights={"provider-A": 100})

    ledger.reserve("res-1", "consumer-1", "consumer_available", 400)
    ledger.consume_reservation("res-1", 400)
    assert ledger.balance("consumer-1/settlement_payable") == 400

    ledger.settle_success("S1", "consumer-1", ["provider-A"], 400, contract, "J-1")

    # The payable is fully drained and the payer kept the unreserved remainder.
    assert ledger.balance("consumer-1/settlement_payable") == 0
    assert ledger.balance("consumer-1") == 600
    assert ledger.balance("provider-A/pending") == 400
    assert ledger.total_supply() == 1_000


def test_settlement_falls_back_to_balance_when_no_payable() -> None:
    """Without a reservation the charge still comes from the spendable balance."""
    ledger = Ledger()
    ledger.issue("consumer-1", "consumer_available", 1_000)
    ledger.account("provider-A/pending", "provider_pending")
    contract = AllocationContract(contract_id="c", version=1, weights={"provider-A": 100})
    ledger.settle_success("S1", "consumer-1", ["provider-A"], 400, contract, "J-1")
    assert ledger.balance("consumer-1") == 600
    assert ledger.total_supply() == 1_000


def test_downward_correction_refunds_the_payer() -> None:
    """Reducing a settled total credits the payer the difference."""
    ledger = Ledger()
    ledger.issue("consumer-1", "consumer_available", 10_000)
    ledger.account("provider-A/pending", "provider_pending")
    contract = AllocationContract(contract_id="c", version=1, weights={"provider-A": 100})
    ledger.settle_success("S1", "consumer-1", ["provider-A"], 1_000, contract, "J-1")
    ledger.settle_success(
        "S1", "consumer-1", ["provider-A"], 600, contract, "J-2",
        already_recorded={"provider-A": 1_000},
    )
    assert ledger.balance("provider-A/pending") == 600
    assert ledger.balance("consumer-1") == 9_400
    assert ledger.total_supply() == 10_000


def _registry_with_bound_device() -> tuple[IdentityRegistry, object, object, object]:
    """Return a registry with a device key bound to provider-B."""
    registry = IdentityRegistry()
    device_public, device_private = generate_key_pair()
    registry.register_device_key(
        "dk", "device-B", "provider-B", device_public, KEY_AT
    )
    platform_public, platform_private = generate_key_pair()
    registry.register_key(
        "sk", "platform-1", "platform_settlement", platform_public, KEY_AT
    )
    return (
        registry,
        load_private_key(device_private),
        load_private_key(platform_private),
        platform_public,
    )


def _bound_report(private_key: object, provider_id: str = "provider-B") -> object:
    now = dt.datetime.now(dt.UTC)
    measurement = Measurement(
        measurement_id="m-1",
        scope="upstream_call",
        semantics="terminal_total",
        counter_epoch="epoch_1",
        revision=1,
        usage=NormalizedUsage(input_total=800, output_total=200),
        attempt_id="A1",
    )
    return UsageReport(
        report_id="rep-1",
        permit_id="permit-1",
        permit_hash="permit-hash",
        acceptance_hash="acceptance-hash",
        attempt_id="A1",
        provider_id=provider_id,
        device_id="device-B",
        measurement=measurement,
        result_commitment=None,
        final_event_seq=1,
        outcome="completed",
        reported_at=now,
        key_id="dk",
    ).sign(private_key, "dk")


def test_device_cannot_redirect_earnings_to_another_provider(keyring) -> None:
    """A device bound to provider-B must not settle under a provider-C contract."""
    registry, device_private, platform_private, _ = _registry_with_bound_device()
    # The engine verifies the consumer authorization before the device binding,
    # so this registry needs a consumer key. Register the shared keyring's
    # consumer public half and sign a real authorization with its private half.
    registry.register_key(
        "consumer-key-1", "consumer-1", "consumer", keyring.public["consumer-key-1"],
        registered_at=SETTLEMENT_AT - dt.timedelta(days=1),
    )
    ledger = Ledger()
    ledger.issue("consumer-1", "consumer_available", 50_000)
    ledger.account("provider-C/pending", "provider_pending")
    contract = AllocationContract(contract_id="c", version=1, weights={"provider-C": 100})
    engine = SettlementEngine(ledger=ledger, registry=registry, log=MerkleLog())

    with pytest.raises(SettlementError, match=r"may not redirect earnings"):
        engine.settle(
            request_id="R-1",
            service_unit_id="S-1",
            authorization=signed_authorization(
                keyring, request_id="R-1", payer_account_id="consumer-1",
                consumer_id="consumer-1",
            ),
            report=_bound_report(device_private),
            contract=contract,
            contract_hash=contract_hash(contract),
            settlement_private_key=platform_private,
            winner_attempt_id="A1",
            settled_at=SETTLEMENT_AT,
        )
    assert ledger.balance("provider-C/pending") == 0
    assert ledger.balance("consumer-1") == 50_000


def test_unbound_device_cannot_settle(keyring) -> None:
    """A device with no provider binding is refused rather than trusted."""
    registry = IdentityRegistry()
    device_public, device_private = generate_key_pair()
    registry.register_key(
        "dk", "device-B", "provider_device", device_public, KEY_AT
    )
    registry.register_key(
        "consumer-key-1", "consumer-1", "consumer", keyring.public["consumer-key-1"],
        registered_at=SETTLEMENT_AT - dt.timedelta(days=1),
    )
    platform_public, platform_private = generate_key_pair()
    registry.register_key(
        "sk", "platform-1", "platform_settlement", platform_public, KEY_AT
    )
    ledger = Ledger()
    ledger.issue("consumer-1", "consumer_available", 50_000)
    ledger.account("provider-B/pending", "provider_pending")
    contract = AllocationContract(contract_id="c", version=1, weights={"provider-B": 100})
    engine = SettlementEngine(ledger=ledger, registry=registry, log=MerkleLog())

    with pytest.raises(SettlementError, match="no registered provider binding"):
        engine.settle(
            request_id="R-1",
            service_unit_id="S-1",
            authorization=signed_authorization(
                keyring, request_id="R-1", payer_account_id="consumer-1",
                consumer_id="consumer-1",
            ),
            report=_bound_report(load_private_key(device_private)),
            contract=contract,
            contract_hash=contract_hash(contract),
            settlement_private_key=load_private_key(platform_private),
            winner_attempt_id="A1",
            settled_at=SETTLEMENT_AT,
        )
    assert ledger.balance("consumer-1") == 50_000


def test_device_cannot_be_rebound_to_another_provider() -> None:
    """Re-pointing a bound device at a different provider is refused."""
    registry = IdentityRegistry()
    device_public, _ = generate_key_pair()
    registry.register_device_key(
        "dk", "device-B", "provider-B", device_public, KEY_AT
    )
    with pytest.raises(IdentityBindingError, match=r"already bound|is bound to"):
        registry.register_device_key(
            "dk2", "device-B", "provider-C", device_public, KEY_AT
        )


def test_unsigned_checkpoint_degrades_to_unknown() -> None:
    """An unsigned checkpoint must not crash the verifier.

    `verify_offline` promises missing evidence yields a verdict rather than an
    exception, so attacker-supplied checkpoint data cannot be used to crash it.
    """
    registry = IdentityRegistry()
    log = MerkleLog()
    log.append("sha256:" + "a" * 64, dt.datetime.now(dt.UTC))
    unsigned = Checkpoint(
        platform_id="platform-1",
        tree_size=log.size(),
        root_hash=log.root(),
        checkpoint_epoch=1,
        issued_at=dt.datetime.now(dt.UTC),
        key_id="sk",
    )
    # The key point is that this returns rather than raising.
    report = verify_offline(
        VerificationInput(registry=registry, trusted_checkpoint=unsigned)
    )
    assert report.ledger_included == "unknown"

    # With a key supplied, the unsigned checkpoint is still rejected on its
    # merits, and the reason is recorded rather than swallowed.
    platform_public, _ = generate_key_pair()
    registry.register_key(
        "sk", "platform-1", "platform_settlement", platform_public, KEY_AT
    )
    checked = verify_offline(
        VerificationInput(
            registry=registry,
            trusted_checkpoint=unsigned,
            checkpoint_public_key=registry.get_key("sk").public_key(),
        )
    )
    assert checked.ledger_included == "unknown"
    assert any(
        "carries no signature" in anomaly or "does not verify" in anomaly
        for anomaly in checked.anomalies
    ), checked.anomalies