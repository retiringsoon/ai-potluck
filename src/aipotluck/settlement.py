"""Settlement orchestration: from signed reports to a signed receipt.

Why this lives in the client package: the contributor's connector builds and
signs its usage report through this module, and receipt verification recomputes
a settlement from it — construction and re-checking are protocol primitives.
Center bookkeeping and the gateway live in ``ai-potluck-server``, which imports
the same engine to keep its own books.

The order of operations is the security-relevant part. The ledger transaction
commits first; the receipt is signed afterwards and delivered through an outbox.
Signing a "settled" receipt before the money moves would let a platform produce
proof of payment for credit it never paid.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, cast

from .allocation import AllocationContract
from .canonical import canonical_digest
from .errors import (
    AllocationError,
    ConflictError,
    IdentityBindingError,
    InsufficientFundsError,
    LedgerError,
    SettlementError,
    SignatureError,
    VerificationError,
)
from .identity import IdentityRegistry
from .ledger import Journal, Ledger
from .metering import Measurement
from .records import (
    SCHEMA_AUTHORIZATION,
    SCHEMA_SETTLEMENT,
    SCHEMA_USAGE,
    SettlementReceipt,
    SignedRecord,
    UsageReport,
    VerificationPolicy,
    new_nonce,
    verify_chain,
)
from .transparency import Checkpoint, LogEntry, MerkleLog
from .verify import meets_evidence_policy


@dataclass
class SettlementResult:
    """Everything produced by settling one service unit.

    `allocations` is handed out as a fresh copy on every return, so a caller
    editing it cannot corrupt the cached settlement or the ledger's journals.
    `journal` is the ledger's own record and is shared by reference; it is a
    posted journal and must be treated as immutable.
    """

    journal: Journal
    receipt: SignedRecord
    log_index: int
    inclusion_proof: object
    allocations: dict[str, int]

    @property
    def journal_id(self) -> str:
        """Return the settlement journal ID."""
        return self.journal.journal_id

    @property
    def receipt_hash(self) -> str:
        """Return the canonical receipt digest."""
        return self.receipt.hash


@dataclass
class SettlementEngine:
    """Turns accepted usage reports into ledger journals and signed receipts."""

    ledger: Ledger
    registry: IdentityRegistry
    log: MerkleLog
    platform_id: str = "platform-1"
    settlement_key_id: str = "settlement-key-1"
    _receipt_counter: int = 0
    _outbox: list[SignedRecord] = field(default_factory=list)
    _previous_receipt_hash: str | None = None
    #: Business key -> (input fingerprint, result). Makes settlement a function
    #: of its inputs so a retry cannot mint a second receipt for one payment.
    _settled: dict[tuple[str, int], tuple[str, SettlementResult]] = field(
        default_factory=dict
    )

    def settle(
        self,
        request_id: str,
        service_unit_id: str,
        authorization: SignedRecord,
        report: SignedRecord,
        contract: AllocationContract,
        contract_hash: str,
        settlement_private_key: object,
        winner_attempt_id: str,
        billable_tokens: int | None = None,
        result_commitment: str | None = None,
        settlement_revision: int = 1,
        already_recorded: dict[str, int] | None = None,
        settled_at: dt.datetime | None = None,
    ) -> SettlementResult:
        """Settle one service unit and issue a signed receipt.

        The payer, pricing version, and billing basis all come from the
        pre-authorization rather than from the caller. They are facts the
        consumer already signed, so accepting them as separate arguments would
        let a caller bill an account, rate, or basis the consumer never agreed
        to, and would let the two representations drift apart.

        Args:
            request_id: The logical request.
            service_unit_id: The billable unit; equals the request for MVP.
            authorization: The consumer's signed pre-authorization. Its
                signature, validity window, budget, and evidence policy are all
                enforced before any credit moves.
            report: The signed usage report for the winning attempt.
            contract: The allocation contract fixed before dispatch.
            contract_hash: The contract digest bound into the receipt.
            settlement_private_key: The platform settlement key.
            winner_attempt_id: The attempt whose success is being billed.
            billable_tokens: Override for the billed token count; defaults to the
                report's measurement total.
            result_commitment: Optional commitment to the delivered result.
            settlement_revision: Revision of this settlement. A revision above 1
                is a correction and must supply `already_recorded`.
            already_recorded: Per-beneficiary amounts already booked, so a
                correction posts only the net difference.
            settled_at: Settlement instant; defaults to now. Pass an explicit
                value for deterministic tests.

        Returns:
            The settlement result including the receipt and its log position.

        Raises:
            SettlementError: If the authorization or usage report fails
                validation, if the report is not for the winning attempt, or if
                the ledger transaction fails.
        """
        moment = settled_at or dt.datetime.now(dt.UTC)
        # A settlement is identified by (service unit, revision), not by how many
        # times it was attempted. A retry after a lost acknowledgement must
        # return the very same receipt and log position; minting a second
        # receipt for one payment would let an offline earnings summary count the
        # same service twice.
        #
        # The fingerprint covers the economic facts, deliberately not the
        # authorization's or report's own digests: those carry a fresh nonce on
        # every signing, so including them would make a legitimate retry look
        # like different content. What must not change silently is who pays, how
        # much, for which unit, and to whom.
        business_key = (service_unit_id, settlement_revision)
        fingerprint = canonical_digest(
            {
                "request_id": request_id,
                "payer": str(authorization.payload.get("payer_account_id", "")),
                "billable_tokens": (
                    billable_tokens
                    if billable_tokens is not None
                    else _report_tokens(report)
                ),
                "contract": contract_hash,
                "winner_attempt_id": winner_attempt_id,
                "already_recorded": dict(sorted((already_recorded or {}).items())),
            }
        )
        settled = self._settled.get(business_key)
        if settled is not None:
            recorded_fingerprint, recorded_result = settled
            if recorded_fingerprint != fingerprint:
                raise ConflictError(
                    f"service unit {service_unit_id} revision {settlement_revision} was "
                    "already settled with different content; a business key may not be "
                    "reused for a different settlement"
                )
            # Hand back a copy of the allocation map so a caller mutating the
            # result cannot corrupt the cached settlement or the ledger.
            return replace(recorded_result, allocations=dict(recorded_result.allocations))

        fields = self._validate_authorization(authorization, request_id, moment)
        payer_account_id = fields["payer_account_id"]
        pricing_version = fields["pricing_version"]
        billing_basis = fields["billing_basis"]

        # The report's own fields are attacker-controlled, so a schema string and
        # an attempt ID prove nothing. Settling moves real credit, so the
        # device's signature is verified against the registry before any money
        # moves: a self-declared key, a revoked key, or a device signing for a
        # different provider must all fail here rather than in a later audit.
        device_id = report.payload.get("device_id")
        provider_id = report.payload.get("provider_id")
        if not isinstance(device_id, str) or not isinstance(provider_id, str):
            raise SettlementError("usage report does not name a device and a provider")
        try:
            verify_chain(
                self.registry, report, "provider_device", owner_id=device_id,
                witnessed_at=moment,
            )
            self.registry.check_device_binding(device_id, provider_id)
        except SignatureError as exc:
            # Settlement reports one failure type, so a caller handling this call
            # does not have to catch the signature subtypes to learn the report
            # was unacceptable.
            raise SettlementError(
                f"usage report {report.record_id} failed verification: {exc}"
            ) from exc
        # The device binding only constrains who the report *claims* to serve.
        # The credit is actually paid to whoever the allocation contract names,
        # so a device bound to provider-B could otherwise settle under a
        # provider-C contract and redirect its earnings. Require the bound
        # provider to be an actual beneficiary of this settlement.
        beneficiaries = set(contract.all_weights())
        if provider_id not in beneficiaries:
            raise SettlementError(
                f"device {device_id} is bound to {provider_id}, which is not a "
                f"beneficiary of contract {contract.contract_id} "
                f"v{contract.version} ({sorted(beneficiaries)}); "
                "a device may not redirect earnings to another account"
            )
        if report.schema != SCHEMA_USAGE:
            raise SettlementError(
                f"expected a usage report, got schema {report.schema}"
            )
        measurement = report.payload.get("measurement")
        if not isinstance(measurement, dict):
            raise SettlementError("usage report carries no measurement object")
        if measurement.get("attempt_id") != winner_attempt_id:
            raise SettlementError(
                f"usage report is for attempt {measurement.get('attempt_id')}, "
                f"but the winner is {winner_attempt_id}"
            )
        tokens = (
            billable_tokens
            if billable_tokens is not None
            else _measurement_total(measurement)
        )
        self._check_within_budget(
            tokens, fields["max_charge_milli_credits"], authorization.record_id
        )
        self._check_evidence_policy(measurement, fields["minimum_evidence_level"])

        payees = sorted(beneficiaries)
        # The journal id is derived from the business key rather than a process
        # counter: a counter restarts at 1 with each engine instance, so a second
        # engine over the same ledger would collide with an existing journal and
        # be refused. Deriving it also makes a replay produce the same id.
        journal_id = f"J-{service_unit_id}-v{settlement_revision}"
        try:
            journal, allocations = self.ledger.settle_success(
                service_unit_id=service_unit_id,
                payer_account_id=payer_account_id,
                beneficiaries=payees,
                billable_milli_credits=tokens,
                contract=contract,
                journal_id=journal_id,
                version=settlement_revision,
                already_recorded=already_recorded,
            )
        except (InsufficientFundsError, AllocationError, LedgerError) as exc:
            raise SettlementError(
                f"ledger transaction for {service_unit_id} failed: {exc}"
            ) from exc

        self._receipt_counter += 1
        receipt = SettlementReceipt(
            receipt_id=f"R-{self._receipt_counter}",
            platform_id=self.platform_id,
            journal_id=journal.journal_id,
            settlement_revision=settlement_revision,
            authorization_hash=authorization.hash,
            request_id=request_id,
            service_unit_id=service_unit_id,
            winner_attempt_id=winner_attempt_id,
            billing_basis=billing_basis,
            verification_level=cast(
                VerificationPolicy, str(measurement["usage"]["verification_level"])
            ),
            pricing_version=pricing_version,
            allocation_contract_hash=contract_hash,
            billable_tokens=tokens,
            postings=tuple((posting.account, posting.delta) for posting in journal.postings),
            result_commitment=result_commitment,
            previous_receipt_hash=self._previous_receipt_hash,
            nonce=new_nonce(),
            settled_at=moment,
            key_id=self.settlement_key_id,
        )
        signed = receipt.sign(settlement_private_key, self.settlement_key_id)
        self._previous_receipt_hash = signed.hash

        log_index = self.log.append(signed.hash, receipt.settled_at)
        proof = self.log.inclusion_proof(log_index)
        self._outbox.append(signed)
        result = SettlementResult(
            journal=journal,
            receipt=signed,
            log_index=log_index,
            inclusion_proof=proof,
            allocations=allocations,
        )
        self._settled[business_key] = (fingerprint, result)
        return replace(result, allocations=dict(result.allocations))

    def _verify_authorization_signature(
        self,
        authorization: SignedRecord,
        auth_method: str,
        payload: Mapping[str, Any],
        witnessed_at: dt.datetime,
    ) -> None:
        """Verify the authorization signature against the registry's binding.

        Both branches verify against the identity the *registry* binds the key
        to, never the identity the record claims. A consumer-signed
        authorization must additionally be signed by the consumer it names, or
        one consumer's key could authorize a charge on another's behalf. A
        gateway-authenticated one is signed by the platform, and the receipt
        keeps that label rather than presenting it as a consumer signature.

        Args:
            authorization: The signed record under test.
            auth_method: The declared authentication method.
            payload: The signed payload.
            witnessed_at: The platform-controlled instant this message was
                accepted, used for key-validity admission checks.

        Raises:
            IdentityBindingError: If a consumer-signed record names a consumer
                other than the key's owner.
            UnknownKeyError: If the signing key is not registered.
            KeyRevokedError: If the key was not valid when the record was signed.
            SignatureError: If the signature does not verify.
        """
        key = self.registry.get_key(authorization.signer_key_id)
        if auth_method == "consumer_signed":
            verify_chain(
                self.registry, authorization, "consumer", owner_id=key.owner_id,
                witnessed_at=witnessed_at,
            )
            consumer_id = str(payload.get("consumer_id", ""))
            if key.owner_id != consumer_id:
                raise IdentityBindingError(
                    f"authorization {authorization.record_id} claims consumer "
                    f"{consumer_id!r}, but key {key.key_id} is bound to {key.owner_id!r}"
                )
        else:
            verify_chain(
                self.registry, authorization, "platform_settlement", owner_id=key.owner_id,
                witnessed_at=witnessed_at,
            )

    def _validate_authorization(
        self,
        authorization: SignedRecord,
        request_id: str,
        moment: dt.datetime,
    ) -> dict[str, Any]:
        """Verify a consumer authorization and return the terms it fixes.

        Every check here is fail-closed: an authorization that is unsigned,
        expired, for a different request, or unrecognised is refused rather than
        settled with a caveat.

        Args:
            authorization: The signed authorization record.
            request_id: The request being settled.
            moment: The instant settlement is being attempted.

        Returns:
            The payer, pricing version, billing basis, budget cap, and minimum
            evidence level, taken from the signed payload.

        Raises:
            SettlementError: If the record is not an authorization, its signature
                does not verify, it has expired, or it names another request.
        """
        if authorization.schema != SCHEMA_AUTHORIZATION:
            raise SettlementError(
                f"expected a spend authorization, got schema {authorization.schema}"
            )
        payload = authorization.payload
        auth_method = str(payload.get("auth_method", "consumer_signed"))
        # Signature failures are reported as settlement failures: a caller that
        # handles this one call should not have to catch five signature subtypes
        # to learn the authorization was unacceptable.
        try:
            self._verify_authorization_signature(
                authorization, auth_method, payload, moment
            )
        except SignatureError as exc:
            raise SettlementError(
                f"authorization {authorization.record_id} failed verification: {exc}"
            ) from exc

        issued_at = _parse_instant(payload.get("issued_at"), "issued_at")
        expires_at = _parse_instant(payload.get("expires_at"), "expires_at")
        if moment < issued_at:
            raise SettlementError(
                f"authorization {authorization.record_id} is not yet valid: "
                f"issued {issued_at.isoformat()}, settlement attempted {moment.isoformat()}"
            )
        if moment >= expires_at:
            raise SettlementError(
                f"authorization {authorization.record_id} expired at "
                f"{expires_at.isoformat()}; settlement attempted {moment.isoformat()}"
            )
        authorized_request = str(payload.get("request_id", ""))
        if authorized_request != request_id:
            raise SettlementError(
                f"authorization {authorization.record_id} covers request "
                f"{authorized_request!r}, but settlement is for {request_id!r}"
            )
        # The consumer's signed cap, validated before it is used.
        raw_cap = payload.get("max_charge_milli_credits")
        if raw_cap is None:
            raise SettlementError(
                f"authorization {authorization.record_id} carries no budget cap"
            )
        try:
            max_charge_milli_credits = int(str(raw_cap))
        except ValueError as exc:
            raise SettlementError(
                f"authorization {authorization.record_id} has a non-integer budget cap "
                f"{raw_cap!r}"
            ) from exc
        if max_charge_milli_credits <= 0:
            raise SettlementError(
                f"authorization {authorization.record_id} has a non-positive budget cap "
                f"{max_charge_milli_credits}"
            )

        payer = payload.get("payer_account_id")
        if not isinstance(payer, str) or not payer:
            raise SettlementError(
                f"authorization {authorization.record_id} names no payer account"
            )
        # A valid signature proves who signed, not whose funds may be used. The
        # consumer may only charge its own account or one an explicit, live grant
        # covers; otherwise any registered consumer key could drain any funded
        # account by naming it as payer.
        consumer_id = str(payload.get("consumer_id", ""))
        try:
            self.registry.check_spend_authority(
                spender_id=consumer_id,
                payer_account_id=payer,
                amount=max_charge_milli_credits,
                moment=moment,
            )
        except IdentityBindingError as exc:
            raise SettlementError(
                f"authorization {authorization.record_id} cannot be honoured: {exc}"
            ) from exc
        return {
            "payer_account_id": payer,
            "pricing_version": str(payload.get("pricing_version", "")),
            "billing_basis": str(payload.get("billing_basis", "reported_native_usage")),
            "max_charge_milli_credits": max_charge_milli_credits,
            "minimum_evidence_level": str(payload.get("minimum_evidence_level", "node_reported")),
        }

    @staticmethod
    def _check_within_budget(
        tokens: int, max_charge_milli_credits: int, authorization_id: str
    ) -> None:
        """Refuse a charge above what the consumer authorized.

        Args:
            tokens: Milli-credits about to be billed.
            max_charge_milli_credits: The cap the consumer signed.
            authorization_id: Used in the error message.

        Raises:
            SettlementError: If the charge exceeds the cap.
        """
        if tokens > max_charge_milli_credits:
            raise SettlementError(
                f"billable {tokens} milli-credits exceeds the {max_charge_milli_credits} "
                f"authorized by {authorization_id}; the excess stays a resource cost "
                "and must not be charged to the consumer"
            )

    @staticmethod
    def _check_evidence_policy(
        measurement: Mapping[str, Any], minimum_level: str
    ) -> None:
        """Refuse a report whose evidence is weaker than the consumer demanded.

        Args:
            measurement: The usage report's measurement object.
            minimum_level: The level the authorization required.

        Raises:
            SettlementError: If the measurement carries no usable evidence level
                or it falls below the policy.
        """
        usage = measurement.get("usage")
        if not isinstance(usage, Mapping):
            raise SettlementError("usage report measurement has no usage object")
        actual = str(usage.get("verification_level", ""))
        try:
            sufficient = meets_evidence_policy(actual, minimum_level)
        except VerificationError as exc:
            raise SettlementError(f"cannot evaluate evidence policy: {exc}") from exc
        if not sufficient:
            raise SettlementError(
                f"usage report evidence level {actual!r} does not meet the "
                f"{minimum_level!r} minimum the authorization required"
            )

    def sign_checkpoint(
        self,
        private_key: object,
        epoch: int,
    ) -> Checkpoint:
        """Sign a transparency-log checkpoint over the current tree."""
        return Checkpoint(
            platform_id=self.platform_id,
            tree_size=self.log.size(),
            root_hash=self.log.root(),
            checkpoint_epoch=epoch,
            issued_at=dt.datetime.now(dt.UTC),
            key_id=self.settlement_key_id,
        ).sign(private_key)

    def redeliver(self, receipt_id: str) -> SignedRecord:
        """Re-deliver an already-issued receipt without settling twice.

        Args:
            receipt_id: The receipt to redeliver.

        Returns:
            The same signed receipt.

        Raises:
            SettlementError: If the receipt was never issued.
        """
        for record in self._outbox:
            if record.record_id == receipt_id:
                return record
        raise SettlementError(f"receipt {receipt_id} was never issued by this engine")

    def outbox(self) -> list[SignedRecord]:
        """Return receipts awaiting delivery."""
        return list(self._outbox)

    def resume_chain(self, previous_receipt_hash: str | None) -> None:
        """Continue the receipt hash chain after a restart.

        Each receipt names its predecessor, so a gap would let two honest
        verifiers disagree about which receipts belong to the log. A gateway that
        rebuilds itself from durable state calls this once with the hash of the
        last receipt it recorded.

        Args:
            previous_receipt_hash: Hash of the last issued receipt, or None when
                the engine starts on an empty log.
        """
        self._previous_receipt_hash = previous_receipt_hash

    def log_entries(self) -> list[LogEntry]:
        """Return transparency-log entries."""
        return self.log.entries()


def _report_tokens(report: SignedRecord) -> int:
    """Return a usage report's normalized token total.

    Used only to fingerprint a settlement before validating the report, so it
    reads the measurement without trusting it: validation still decides whether
    the report is acceptable.

    Args:
        report: The signed usage report.

    Returns:
        Input plus output tokens.

    Raises:
        SettlementError: If the report carries no usable measurement. This runs
            before signature validation, so an unauthenticated caller must not be
            able to provoke a non-domain exception out of it.
    """
    measurement = report.payload.get("measurement")
    if not isinstance(measurement, Mapping):
        raise SettlementError("usage report carries no measurement object")
    return _measurement_total(measurement)


def _parse_instant(value: object, field_name: str) -> dt.datetime:
    """Parse an ISO-8601 instant from a signed payload.

    Args:
        value: The raw payload field.
        field_name: Used in the error message.

    Returns:
        A timezone-aware UTC datetime.

    Raises:
        SettlementError: If the field is missing, not a string, or unparseable.
    """
    if not isinstance(value, str):
        raise SettlementError(f"authorization field {field_name} is missing or not a string")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise SettlementError(f"authorization field {field_name} is not ISO-8601: {exc}") from exc
    if parsed.tzinfo is None:
        raise SettlementError(f"authorization field {field_name} must carry a timezone")
    return parsed.astimezone(dt.UTC)


def _measurement_total(measurement: Mapping[str, Any]) -> int:
    """Return a measurement's input plus output token total.

    Args:
        measurement: The usage report's measurement object.

    Returns:
        The normalized token total.

    Raises:
        SettlementError: If the object is not a usable measurement. Every field
            is checked rather than assumed: this runs before signature
            validation, so anyone can reach it and must not be able to provoke a
            raw `KeyError` out of the settlement path.
    """
    usage = measurement.get("usage")
    if not isinstance(usage, Mapping):
        raise SettlementError("usage report measurement has no usage object")
    parts: list[int] = []
    for name in ("input_total", "output_total"):
        raw = usage.get(name)
        if raw is None:
            raise SettlementError(f"usage report measurement has no {name}")
        try:
            parts.append(int(str(raw)))
        except ValueError as exc:
            raise SettlementError(
                f"usage report measurement {name} is not an integer: {raw!r}"
            ) from exc
    return sum(parts)


def build_usage_report(
    report_id: str,
    permit_hash: str,
    acceptance_hash: str,
    attempt_id: str,
    provider_id: str,
    device_id: str,
    measurement: Measurement,
    outcome: str,
    device_private_key: object,
    device_key_id: str,
    reported_at: dt.datetime | None = None,
    revision: int = 1,
    previous_report_hash: str | None = None,
    final_event_seq: int = 0,
    result_commitment: str | None = None,
) -> SignedRecord:
    """Construct and sign a usage report.

    Args:
        report_id: The report ID.
        permit_hash: Digest of the dispatch permit.
        acceptance_hash: Digest of the provider acceptance.
        attempt_id: The attempt measured.
        provider_id: The contributing provider account that earns the credit.
        device_id: The device holding the signing key.
        measurement: The normalized measurement.
        outcome: One of completed, failed, cancelled, outcome_unknown.
        device_private_key: The device signing key.
        device_key_id: The device key ID.
        reported_at: Report time; defaults to now.
        revision: Revision number, for corrections.
        previous_report_hash: Required when revision is above 1.
        final_event_seq: Last emitted sequence number.
        result_commitment: Optional result commitment.

    Returns:
        The signed record.

    Raises:
        ValueError: If the outcome is not a valid literal.
    """
    if outcome not in {"completed", "failed", "cancelled", "outcome_unknown"}:
        raise ValueError(f"invalid outcome: {outcome}")
    report = UsageReport(
        report_id=report_id,
        permit_id=permit_hash,
        permit_hash=permit_hash,
        acceptance_hash=acceptance_hash,
        attempt_id=attempt_id,
        provider_id=provider_id,
        device_id=device_id,
        measurement=measurement,
        result_commitment=result_commitment,
        final_event_seq=final_event_seq,
        outcome=outcome,  # type: ignore[arg-type]
        reported_at=reported_at or dt.datetime.now(dt.UTC),
        revision=revision,
        previous_report_hash=previous_report_hash,
        key_id=device_key_id,
    )
    return report.sign(device_private_key, device_key_id)


def receipt_schema() -> str:
    """Return the settlement receipt schema identifier."""
    return SCHEMA_SETTLEMENT