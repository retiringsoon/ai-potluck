"""Trust-boundary regressions.

Each test here pins a defect that let untrusted or out-of-window data through a
boundary that is supposed to stop it. They assert concrete before/after values
rather than merely "raises something", because the failures were silent: the
boundary reported success, or reported success too early, while accepting input
it should never have accepted.

- `TestStrictParsingRejectsNonFiniteNumbers`  NaN/Infinity bypassed strict parsing
- `TestCanonicalKeyOrdering`                 key order was code point, not UTF-16
- `TestRevocationWindow`                      the revocation window was dead code
- `TestInclusionNeedsTrustedRoot`            a forged log could vouch for itself
"""

from __future__ import annotations

import datetime as dt
import math

import pytest
from conftest import SETTLEMENT_AT, contract_hash, make_measurement, solo_contract

from aipotluck.canonical import base64_decode, canonicalize, loads_strict
from aipotluck.errors import NonCanonicalNumberError
from aipotluck.identity import IdentityRegistry, generate_key_pair
from aipotluck.ledger import Ledger
from aipotluck.records import DispatchPermit, ProviderAcceptance, SpendAuthorization, new_nonce
from aipotluck.settlement import SettlementEngine, build_usage_report
from aipotluck.statemachine import RecoveryContext, decide_recovery, replay_costs_tokens
from aipotluck.transparency import Checkpoint, MerkleLog
from aipotluck.verify import VerificationInput, verify_offline

# Two keys whose UTF-16 order and code-point order disagree. U+1F600 is a
# non-BMP character, so UTF-16 encodes it as the surrogate pair D83D DE00.
# D83D < E000, so RFC 8785 requires U+1F600 first; code points put U+E000 first.
PRIVATE_USE = chr(0xE000)
GRINNING_FACE = chr(0x1F600)


def _at(year: int, month: int, day: int) -> dt.datetime:
    return dt.datetime(year, month, day, tzinfo=dt.UTC)


# -- 1.1 NaN and Infinity bypassed strict parsing --------------------------


class TestStrictParsingRejectsNonFiniteNumbers:
    """`loads_strict` hooked `parse_float` but not `parse_constant`.

    Python's json module routes `NaN`, `Infinity`, and `-Infinity` to
    `parse_constant` rather than `parse_float`, so all three reached the caller
    as live floats. That contradicted the module's own rule that floats are
    rejected outright, and it let a non-canonical document through a boundary
    whose entire purpose is canonicality.
    """

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_literal_is_rejected(self, literal: str) -> None:
        """All three non-finite literals must be refused."""
        with pytest.raises(NonCanonicalNumberError):
            loads_strict(f'{{"amount": {literal}}}')

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_literal_is_rejected_in_nested_position(self, literal: str) -> None:
        """Nesting must not provide a way around the check."""
        with pytest.raises(NonCanonicalNumberError):
            loads_strict(f'{{"a": [{{"b": {literal}}}]}}')

    @pytest.mark.parametrize(
        ("literal", "expected"),
        [("NaN", math.nan), ("Infinity", math.inf), ("-Infinity", -math.inf)],
    )
    def test_defect_value_json_module_would_have_accepted(
        self, literal: str, expected: float
    ) -> None:
        """Pin the exact value the parser used to hand back for each literal.

        `json.loads` with no `parse_constant` still returns these, which is the
        concrete behaviour being closed off. Before the fix, `loads_strict`
        returned the identical value.
        """
        import json

        parsed = json.loads(f'{{"a": {literal}}}')["a"]
        if math.isnan(expected):
            assert isinstance(parsed, float) and math.isnan(parsed)
        else:
            assert parsed == expected
        with pytest.raises(NonCanonicalNumberError):
            loads_strict(f'{{"a": {literal}}}')

    def test_finite_numbers_are_still_accepted(self) -> None:
        """Integers and ordinary JSON numbers must keep parsing."""
        assert loads_strict('{"a": 1, "b": -2, "c": 0}') == {"a": 1, "b": -2, "c": 0}

    def test_non_finite_literal_cannot_reach_canonical_bytes(self) -> None:
        """A non-finite value never becomes canonical bytes that could be signed.

        `canonicalize` already refuses floats, so the danger was a float arriving
        through the parser and then being carried elsewhere. This asserts the
        end-to-end shape: nothing to sign.
        """
        with pytest.raises(NonCanonicalNumberError):
            loads_strict('{"amount": Infinity}')
        with pytest.raises(NonCanonicalNumberError):
            canonicalize({"amount": math.inf})


# -- 1.2 key sort order was not RFC 8785 -----------------------------------


class TestCanonicalKeyOrdering:
    """`canonicalize` sorted keys by Unicode code point.

    RFC 8785 section 3.2.3 requires UTF-16 code-unit order. The two disagree for
    any key outside the BMP, because those characters encode to surrogates in
    the range D800-DFFF, which sorts below U+E000 in UTF-16 while sorting above
    it by code point. Two implementations could therefore produce different
    canonical bytes for the same object, and a signature over those bytes would
    not verify across them.
    """

    def test_non_bmp_key_sorts_before_private_use_key(self) -> None:
        """RFC 8785 order puts U+1F600 (D83D DE00) before U+E000."""
        encoded = canonicalize({PRIVATE_USE: 2, GRINNING_FACE: 1}).decode("utf-8")
        assert encoded == f'{{"{GRINNING_FACE}":1,"{PRIVATE_USE}":2}}'

    def test_code_point_order_would_have_been_the_opposite(self) -> None:
        """Pin the pre-fix ordering so the divergence is documented, not implied.

        By code point U+E000 < U+1F600, which is the order the old code produced.
        By UTF-16 code unit the surrogate lead byte D83D precedes E000, so the
        required order is the reverse.
        """
        assert PRIVATE_USE < GRINNING_FACE
        assert GRINNING_FACE.encode("utf-16-be") < PRIVATE_USE.encode("utf-16-be")
        # The naive code-point sort really is the reverse of the required order.
        assert sorted([PRIVATE_USE, GRINNING_FACE]) == [PRIVATE_USE, GRINNING_FACE]
        assert sorted([PRIVATE_USE, GRINNING_FACE], key=lambda k: k.encode("utf-16-be")) == [
            GRINNING_FACE,
            PRIVATE_USE,
        ]

    def test_ascii_key_order_is_unchanged(self) -> None:
        """ASCII keys are unaffected: both orders agree, and digit < letter."""
        assert (
            canonicalize({"b": 1, "a": 2, "B": 3, "A": 4, "10": 5, "9": 6}).decode("utf-8")
            == '{"10":5,"9":6,"A":4,"B":3,"a":2,"b":1}'
        )

    def test_bmp_key_order_is_unchanged(self) -> None:
        """Keys inside the BMP have identical UTF-16 and code-point order.

        Every BMP character encodes to a single UTF-16 code unit equal to its
        code point, so the two orderings cannot diverge here.
        """
        keys = {"é": 1, "z": 2, "A": 3, "Ω": 4}
        encoded = canonicalize(keys).decode("utf-8")
        assert encoded == '{"A":3,"z":2,"é":1,"Ω":4}'
        # Code-point order agrees with UTF-16 order for every BMP character.
        assert [k for k in sorted(keys)] == ["A", "z", "é", "Ω"]
        assert list(keys) != sorted(keys)  # insertion order differs; sorting is what matters

    def test_ordering_is_stable_and_repeatable(self) -> None:
        """Canonicalization is a pure function of the value."""
        value = {GRINNING_FACE: 1, PRIVATE_USE: 2, "a": 3, "z": 4}
        first = canonicalize(value)
        assert all(canonicalize(value) == first for _ in range(5))

    def test_nested_objects_use_the_same_order(self) -> None:
        """The ordering rule applies at every depth, not just the top level."""
        encoded = canonicalize({"outer": {PRIVATE_USE: 1, GRINNING_FACE: 2}}).decode("utf-8")
        assert encoded == f'{{"outer":{{"{GRINNING_FACE}":2,"{PRIVATE_USE}":1}}}}'


# -- 1.3 the revocation window was dead code -------------------------------


class TestRevocationWindow:
    """`IdentityKey.is_valid_at` short-circuited on `status == "revoked"`.

    That made the `revoked_at` comparison on the following line unreachable, so
    a record signed while the key was still good was rejected after the fact.
    The class docstring already stated the intended semantics; the code did not
    implement them.
    """

    @staticmethod
    def _revoked_key() -> tuple[IdentityRegistry, str]:
        registry = IdentityRegistry()
        public_b64, _private_b64 = generate_key_pair()
        registry.register_key("k1", "owner-1", "consumer", public_b64, _at(2026, 1, 1))
        registry.revoke_key("k1", _at(2026, 1, 3))
        return registry, "k1"

    def test_signed_before_revocation_is_still_valid(self) -> None:
        """The regression: 2026-01-02 returned False and must return True."""
        registry, key_id = self._revoked_key()
        key = registry.get_key(key_id)
        assert key.status == "revoked"
        assert key.is_valid_at(_at(2026, 1, 2)) is True

    def test_signed_at_revocation_moment_is_refused(self) -> None:
        """Revocation is inclusive: at the instant of revocation the key is dead."""
        registry, key_id = self._revoked_key()
        assert registry.get_key(key_id).is_valid_at(_at(2026, 1, 3)) is False

    def test_signed_after_revocation_is_refused(self) -> None:
        """A later signature under a revoked key stays invalid."""
        registry, key_id = self._revoked_key()
        assert registry.get_key(key_id).is_valid_at(_at(2026, 1, 4)) is False

    def test_signed_before_registration_is_refused(self) -> None:
        """The lower bound of the window is still enforced."""
        registry, key_id = self._revoked_key()
        assert registry.get_key(key_id).is_valid_at(_at(2025, 12, 31)) is False

    def test_active_key_is_valid_throughout_its_lifetime(self) -> None:
        """A key that was never revoked stays valid from registration onwards."""
        registry = IdentityRegistry()
        public_b64, _private_b64 = generate_key_pair()
        registry.register_key("k2", "owner-1", "consumer", public_b64, _at(2026, 1, 1))
        key = registry.get_key("k2")
        assert key.is_valid_at(_at(2026, 1, 1)) is True
        assert key.is_valid_at(_at(2027, 6, 1)) is True

    def test_revoked_without_a_timestamp_fails_closed(self) -> None:
        """A revoked key with no `revoked_at` has no window, so it never validates."""
        registry = IdentityRegistry()
        public_b64, _private_b64 = generate_key_pair()
        registry.register_key("k3", "owner-1", "consumer", public_b64, _at(2026, 1, 1))
        key = registry._keys["k3"]
        from dataclasses import replace as _replace

        broken = _replace(key, status="revoked", revoked_at=None)
        assert broken.is_valid_at(_at(2026, 1, 2)) is False

    def test_boundary_is_a_window_not_a_cliff(self) -> None:
        """Every instant strictly before revocation validates; none at or after does."""
        registry, key_id = self._revoked_key()
        key = registry.get_key(key_id)
        registered_at = _at(2026, 1, 1)
        revoked_at = _at(2026, 1, 3)
        # Sweep the whole open window between registration and revocation.
        moment = registered_at
        while moment < revoked_at:
            assert key.is_valid_at(moment) is True, moment
            moment += dt.timedelta(hours=1)
        # The boundary itself and everything after it is closed.
        moment = revoked_at
        while moment < revoked_at + dt.timedelta(days=3):
            assert key.is_valid_at(moment) is False, moment
            moment += dt.timedelta(hours=1)


# -- 1.4 inclusion trusted an attacker-supplied root -----------------------


def _settled(keyring, ledger: Ledger, now: dt.datetime) -> tuple[object, object, MerkleLog, str]:
    """Drive a real receipt through settlement and return it with its log.

    Returns:
        The settlement result, the usage report, the log, and the contract hash.
    """
    authorization = SpendAuthorization(
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
    permit = DispatchPermit(
        permit_id="permit-A1",
        authorization_id="auth-R1",
        authorization_hash=authorization.hash,
        attempt_id="A1",
        dispatch_epoch=1,
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
    acceptance = ProviderAcceptance(
        acceptance_id="acc-A1",
        permit_id="permit-A1",
        permit_hash=permit.hash,
        attempt_id="A1",
        provider_id="provider-B",
        device_id="device-B",
        local_policy_version=3,
        local_reserved_milli_credits=20_000,
        accepted_at=now,
    ).sign(keyring.private["device-key-B"], "device-key-B")

    log = MerkleLog()
    engine = SettlementEngine(ledger=ledger, registry=keyring.registry, log=log)
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
    return result, report, log, contract_hash(contract)


class TestInclusionNeedsTrustedRoot:
    """`ledger_included` compared the folded proof to `proof.root_hash`.

    The fold was computed correctly, but it was compared against a value that
    arrived inside the untrusted proof. An attacker fabricating an entire
    one-leaf log therefore got a self-consistent proof, a `pass` on this
    dimension, and `all_pass` overall. Presence in the log was never actually
    established, because nothing tied the log to any root the verifier already
    believed.
    """

    def test_self_consistent_forged_log_does_not_pass(self, keyring, ledger: Ledger, now) -> None:
        """A fabricated log must not reach `pass` on its own say-so.

        This is the core defect. The forged proof is internally perfect: it
        verifies against its own root via `transparency.verify_inclusion`. Before
        the fix that was enough to yield `ledger_included == "pass"` and
        `all_pass == True`.
        """
        result, _report, real_log, _chash = _settled(keyring, ledger, now)

        # A wholly separate log the attacker built themselves, containing a
        # single fabricated leaf for the real receipt's hash. The receipt is
        # committed at the settlement instant, so the forged leaf is stamped a
        # second later: a single-leaf log's root is its leaf hash, and an
        # identical stamp would make the forgery byte-identical to the honest
        # log rather than a distinct one.
        forged = MerkleLog()
        forged_index = forged.append(result.receipt.hash, now + dt.timedelta(seconds=1))
        forged_proof = forged.inclusion_proof(forged_index)
        forged_entry = forged.entries()[forged_index]

        # Confirm the forgery is self-consistent: this is what made it dangerous.
        from aipotluck.transparency import verify_inclusion as _verify_inclusion

        assert _verify_inclusion(forged_proof, forged_entry.leaf_bytes()) is True

        report = verify_offline(
            VerificationInput(
                registry=keyring.registry,
                receipts=[result.receipt],
                inclusion_proofs={result.receipt.hash: forged_proof},
                leaf_bytes={result.receipt.hash: forged_entry.leaf_bytes()},
                trusted_root=real_log.root(),
            )
        )
        assert report.ledger_included != "pass"
        assert report.all_pass is False

    def test_forged_log_without_any_trusted_root_is_unknown(
        self, keyring, ledger: Ledger, now
    ) -> None:
        """With no trusted root the verdict is `unknown`, never `pass`."""
        result, _report, _log, _chash = _settled(keyring, ledger, now)
        forged = MerkleLog()
        index = forged.append(result.receipt.hash, now)
        proof = forged.inclusion_proof(index)
        entry = forged.entries()[index]

        report = verify_offline(
            VerificationInput(
                registry=keyring.registry,
                receipts=[result.receipt],
                inclusion_proofs={result.receipt.hash: proof},
                leaf_bytes={result.receipt.hash: entry.leaf_bytes()},
            )
        )
        assert report.ledger_included == "unknown"
        assert report.all_pass is False

    def test_missing_trusted_root_is_explained_by_an_anomaly(
        self, keyring, ledger: Ledger, now
    ) -> None:
        """The `unknown` verdict must say why presence is unverified."""
        result, _report, _log, _chash = _settled(keyring, ledger, now)
        forged = MerkleLog()
        index = forged.append(result.receipt.hash, now)
        proof = forged.inclusion_proof(index)
        entry = forged.entries()[index]

        report = verify_offline(
            VerificationInput(
                registry=keyring.registry,
                receipts=[result.receipt],
                inclusion_proofs={result.receipt.hash: proof},
                leaf_bytes={result.receipt.hash: entry.leaf_bytes()},
            )
        )
        assert any("presence in the log is unverified" in a for a in report.anomalies)
        assert any("no trusted log root" in a for a in report.anomalies)

    def test_honest_log_against_the_real_root_passes(
        self, keyring, ledger: Ledger, now
    ) -> None:
        """The fix must not break the honest path: real root means `pass`."""
        result, report_under_test, log, chash = _settled(keyring, ledger, now)
        entry = log.entries()[result.log_index]
        contract = solo_contract("provider-B")

        report = verify_offline(
            VerificationInput(
                registry=keyring.registry,
                receipts=[result.receipt],
                usage_reports=[report_under_test],
                contracts={chash: contract},
                inclusion_proofs={result.receipt.hash: result.inclusion_proof},
                leaf_bytes={result.receipt.hash: entry.leaf_bytes()},
                trusted_root=log.root(),
                expected_receipt_ids=[result.receipt.record_id],
                expected_sequence=[1],
                observed_sequence=[1],
            )
        )
        assert report.ledger_included == "pass"

    def test_real_log_with_a_wrong_trusted_root_fails(
        self, keyring, ledger: Ledger, now
    ) -> None:
        """A genuine proof under the wrong root is a contradiction, not `unknown`."""
        result, _report, log, _chash = _settled(keyring, ledger, now)
        entry = log.entries()[result.log_index]

        report = verify_offline(
            VerificationInput(
                registry=keyring.registry,
                receipts=[result.receipt],
                inclusion_proofs={result.receipt.hash: result.inclusion_proof},
                leaf_bytes={result.receipt.hash: entry.leaf_bytes()},
                trusted_root="sha256:" + "00" * 32,
            )
        )
        assert report.ledger_included == "fail"
        assert any("does not reach the trusted root" in a for a in report.anomalies)

    def test_signed_checkpoint_supplies_the_trusted_root(
        self, keyring, ledger: Ledger, now
    ) -> None:
        """A checkpoint signed by the platform key anchors the root legitimately."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        result, _report, log, _chash = _settled(keyring, ledger, now)
        entry = log.entries()[result.log_index]
        checkpoint = Checkpoint(
            platform_id="platform-1",
            tree_size=log.size(),
            root_hash=log.root(),
            checkpoint_epoch=1,
            issued_at=now,
            key_id="settlement-key-1",
        ).sign(keyring.private["settlement-key-1"])
        platform_public = Ed25519PublicKey.from_public_bytes(
            base64_decode(keyring.public["settlement-key-1"])
        )

        report = verify_offline(
            VerificationInput(
                registry=keyring.registry,
                receipts=[result.receipt],
                inclusion_proofs={result.receipt.hash: result.inclusion_proof},
                leaf_bytes={result.receipt.hash: entry.leaf_bytes()},
                trusted_checkpoint=checkpoint,
                checkpoint_public_key=platform_public,
            )
        )
        assert report.ledger_included == "pass"

    def test_checkpoint_without_a_public_key_does_not_authenticate(
        self, keyring, ledger: Ledger, now
    ) -> None:
        """An unauthenticated checkpoint is no better than the proofs it anchors."""
        result, _report, log, _chash = _settled(keyring, ledger, now)
        entry = log.entries()[result.log_index]
        checkpoint = Checkpoint(
            platform_id="platform-1",
            tree_size=log.size(),
            root_hash=log.root(),
            checkpoint_epoch=1,
            issued_at=now,
            key_id="settlement-key-1",
        ).sign(keyring.private["settlement-key-1"])

        report = verify_offline(
            VerificationInput(
                registry=keyring.registry,
                receipts=[result.receipt],
                inclusion_proofs={result.receipt.hash: result.inclusion_proof},
                leaf_bytes={result.receipt.hash: entry.leaf_bytes()},
                trusted_checkpoint=checkpoint,
            )
        )
        assert report.ledger_included == "unknown"
        assert any("without a checkpoint public key" in a for a in report.anomalies)

    def test_checkpoint_contradicting_trusted_root_is_refused(
        self, keyring, ledger: Ledger, now
    ) -> None:
        """When a checkpoint and a held root disagree, neither is trusted."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        result, _report, log, _chash = _settled(keyring, ledger, now)
        entry = log.entries()[result.log_index]
        lying = Checkpoint(
            platform_id="platform-1",
            tree_size=log.size(),
            root_hash="sha256:" + "11" * 32,
            checkpoint_epoch=1,
            issued_at=now,
            key_id="settlement-key-1",
        ).sign(keyring.private["settlement-key-1"])
        platform_public = Ed25519PublicKey.from_public_bytes(
            base64_decode(keyring.public["settlement-key-1"])
        )

        report = verify_offline(
            VerificationInput(
                registry=keyring.registry,
                receipts=[result.receipt],
                inclusion_proofs={result.receipt.hash: result.inclusion_proof},
                leaf_bytes={result.receipt.hash: entry.leaf_bytes()},
                trusted_checkpoint=lying,
                checkpoint_public_key=platform_public,
                trusted_root=log.root(),
            )
        )
        assert report.ledger_included == "unknown"
        assert any("disagrees with the signed checkpoint root" in a for a in report.anomalies)


# -- Part 2: reachable recovery actions only -------------------------------


class TestRecoveryDecisionTable:
    """`continue_from_partial` was declared but unreachable, and `COMPLETED`
    was shadowed by the `output_committed` branch.

    Enumerating every input combination showed the action was never returned,
    while `COMPLETED` with committed output returned `fail_explicitly` even
    though a completed attempt has a stored result that replays for free.
    """

    def test_completed_with_committed_output_replays_the_result(self) -> None:
        """A finished result must be replayed, not reported as a failure."""
        action = decide_recovery(
            RecoveryContext(
                attempt_status="COMPLETED",
                execution_certainty="submitted",
                output_committed=True,
                buffered_mode=False,
                original_execution_resumable=False,
                input_replayable=True,
                deadline_remaining_ms=30_000,
                has_external_side_effects=False,
                duplicate_execution_policy="bounded_model_only",
            )
        )
        assert action == "replay_result"
        assert replay_costs_tokens(action) is False

    def test_external_side_effects_still_block_a_completed_replay(self) -> None:
        """Side effects are the one condition that still wins over replay."""
        action = decide_recovery(
            RecoveryContext(
                attempt_status="COMPLETED",
                execution_certainty="submitted",
                output_committed=True,
                buffered_mode=False,
                original_execution_resumable=False,
                input_replayable=True,
                deadline_remaining_ms=30_000,
                has_external_side_effects=True,
                duplicate_execution_policy="bounded_model_only",
            )
        )
        assert action == "fail_explicitly"

    def test_committed_output_still_blocks_retry_for_unfinished_attempts(self) -> None:
        """The guard the reorder had to preserve: no second branch behind output."""
        action = decide_recovery(
            RecoveryContext(
                attempt_status="RUNNING",
                execution_certainty="submitted",
                output_committed=True,
                buffered_mode=False,
                original_execution_resumable=False,
                input_replayable=True,
                deadline_remaining_ms=30_000,
                has_external_side_effects=False,
                duplicate_execution_policy="bounded_model_only",
            )
        )
        assert action == "fail_explicitly"

    def test_every_declared_action_is_reachable(self) -> None:
        """No declared action may be dead.

        Sweeps the full cross-product of the decision context and asserts each
        declared action is produced by at least one combination.
        """
        import itertools
        from typing import get_args

        from aipotluck.statemachine import AttemptStatus, ExecutionCertainty, RecoveryAction

        sweep = itertools.product(
            get_args(AttemptStatus),
            get_args(ExecutionCertainty),
            [False, True],
            [False, True],
            [False, True],
            [False, True],
            [-1, 0, 30_000],
            [False, True],
            ["deny", "bounded_model_only"],
        )
        seen = {
            decide_recovery(RecoveryContext(*combination))
            for combination in sweep
        }
        assert seen == set(get_args(RecoveryAction)), (
            f"unreachable actions: {set(get_args(RecoveryAction)) - seen}"
        )