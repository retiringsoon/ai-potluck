"""Settlement must not move credit on an unverified usage report.

The engine's `registry` field exists to bind signatures to identities. If
settlement skips that check, a self-declared key can produce a well-formed
report and get paid, and the fraud only surfaces during a later audit — after
the money has moved.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from conftest import SETTLEMENT_AT, Keyring, contract_hash, signed_authorization

from aipotluck.allocation import AllocationContract
from aipotluck.errors import SettlementError
from aipotluck.identity import generate_key_pair, load_private_key
from aipotluck.ledger import Ledger
from aipotluck.metering import Measurement, NormalizedUsage
from aipotluck.records import UsageReport
from aipotluck.settlement import SettlementEngine
from aipotluck.transparency import MerkleLog


def _contract() -> AllocationContract:
    return AllocationContract(contract_id="c", version=1, weights={"provider-B": 100})


def _report(
    private_key: object, key_id: str, device_id: str, attempt_id: str = "A2"
) -> object:
    now = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
    measurement = Measurement(
        measurement_id="m-1",
        scope="upstream_call",
        semantics="terminal_total",
        counter_epoch="epoch_1",
        revision=1,
        usage=NormalizedUsage(input_total=800, output_total=200),
        attempt_id=attempt_id,
    )
    return UsageReport(
        report_id="rep-1",
        permit_id="permit-1",
        permit_hash="permit-hash",
        acceptance_hash="acceptance-hash",
        attempt_id=attempt_id,
        provider_id="provider-B",
        device_id=device_id,
        measurement=measurement,
        result_commitment=None,
        final_event_seq=1,
        outcome="completed",
        reported_at=now,
        key_id=key_id,
    ).sign(private_key, key_id)


def _engine(ledger: Ledger, keyring: Keyring) -> SettlementEngine:
    return SettlementEngine(ledger=ledger, registry=keyring.registry, log=MerkleLog())


def test_unregistered_key_cannot_settle(ledger: Ledger, keyring: Keyring, now: object) -> None:
    """A report signed by a key outside the registry must not move credit."""
    engine = _engine(ledger, keyring)
    _, rogue_private_b64 = generate_key_pair()
    rogue = load_private_key(rogue_private_b64)
    contract = _contract()

    with pytest.raises(SettlementError, match="is not in the trusted registry"):
        engine.settle(
            request_id="R-1001",
            service_unit_id="R-1001",
            authorization=signed_authorization(
                keyring, request_id="R-1001", payer_account_id="consumer-X"
            ),
            report=_report(rogue, "rogue-key", "rogue-device"),
            contract=contract,
            contract_hash=contract_hash(contract),
            settlement_private_key=rogue,
            winner_attempt_id="A2",
            settled_at=SETTLEMENT_AT,
        )

    assert ledger.balance("consumer-X") == 100_000
    assert ledger.balance("provider-B/pending") == 0
    assert [j for j in ledger.journals() if j.service_unit_id == "R-1001"] == []


def test_device_signing_for_another_provider_is_refused(
    ledger: Ledger, keyring: Keyring, now: object
) -> None:
    """A registered device may not redirect usage to a provider it is not bound to.

    device-B is bound to provider-B. If its report names provider-A instead, the
    earnings would move to an account the device has no claim on.
    """
    engine = _engine(ledger, keyring)
    contract = _contract()
    now_dt = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
    measurement = Measurement(
        measurement_id="m-1",
        scope="upstream_call",
        semantics="terminal_total",
        counter_epoch="epoch_1",
        revision=1,
        usage=NormalizedUsage(input_total=800, output_total=200),
        attempt_id="A2",
    )
    # A well-formed, validly signed report — it simply names the wrong provider.
    rogue = UsageReport(
        report_id="rep-rogue",
        permit_id="permit-1",
        permit_hash="permit-hash",
        acceptance_hash="acceptance-hash",
        attempt_id="A2",
        provider_id="provider-A",
        device_id="device-B",
        measurement=measurement,
        result_commitment=None,
        final_event_seq=1,
        outcome="completed",
        reported_at=now_dt,
        key_id="device-key-B",
    ).sign(keyring.private["device-key-B"], "device-key-B")

    with pytest.raises(SettlementError, match="bound to"):
        engine.settle(
            request_id="R-1001",
            service_unit_id="R-1001",
            authorization=signed_authorization(
                keyring, request_id="R-1001", payer_account_id="consumer-X"
            ),
            report=rogue,
            contract=contract,
            contract_hash=contract_hash(contract),
            settlement_private_key=keyring.private["settlement-key-1"],
            winner_attempt_id="A2",
            settled_at=SETTLEMENT_AT,
        )
    assert ledger.balance("consumer-X") == 100_000
    assert ledger.balance("provider-A/pending") == 0


def test_tampered_report_is_refused(ledger: Ledger, keyring: Keyring, now: object) -> None:
    """Editing the token count after signing must break settlement.

    The forged record is built with `dataclasses.replace` so it is structurally
    identical to an honest one and differs only in the payload. A hand-built
    object would instead fail on a missing attribute, passing without ever
    reaching the signature check.
    """
    engine = _engine(ledger, keyring)
    contract = _contract()
    report = _report(keyring.private["device-key-B"], "device-key-B", "device-B")

    measurement = dict(report.payload["measurement"])
    usage = dict(measurement["usage"])
    usage["input_total"] = 80_000
    measurement["usage"] = usage
    forged = dataclasses.replace(
        report, payload={**dict(report.payload), "measurement": measurement}
    )

    with pytest.raises(SettlementError, match="failed verification"):
        engine.settle(
            request_id="R-1001",
            service_unit_id="R-1001",
            authorization=signed_authorization(
                keyring, request_id="R-1001", payer_account_id="consumer-X"
            ),
            report=forged,
            contract=contract,
            contract_hash=contract_hash(contract),
            settlement_private_key=keyring.private["settlement-key-1"],
            winner_attempt_id="A2",
            settled_at=SETTLEMENT_AT,
        )
    assert ledger.balance("consumer-X") == 100_000