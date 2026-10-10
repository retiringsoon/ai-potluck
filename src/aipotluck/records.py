"""The four signed record types that form an auditable chain.

A. `SpendAuthorization`  — the consumer signs a budget before any work starts.
B. `DispatchPermit` + `ProviderAcceptance` — the coordinator binds an
   authorization to an attempt; the contributor signs acceptance only after its
   own local limits pass.
C. `UsageReport` — the contributor device signs what it measured.
D. `SettlementReceipt` — the platform signs after the ledger transaction
   commits, never before.

Each type has its own domain prefix, so a signed usage report can never be
presented as a settlement receipt. Every record carries `previous_*_hash` so
revisions form a chain, and a revision must reference its predecessor.
"""

from __future__ import annotations

import datetime as dt
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from .canonical import canonical_digest, loads_strict, sign_payload, verify_payload
from .errors import ConflictError, GatewayError, VerificationError
from .identity import IdentityRegistry
from .metering import EVIDENCE_SCOPE, Measurement

SCHEMA_AUTHORIZATION = "aipotluck.spend-authorization.v1"
SCHEMA_PERMIT = "aipotluck.dispatch-permit.v1"
SCHEMA_ACCEPTANCE = "aipotluck.provider-acceptance.v1"
SCHEMA_USAGE = "aipotluck.usage-report.v1"
SCHEMA_SETTLEMENT = "aipotluck.settlement.v1"
SCHEMA_CONTRACT = "aipotluck.allocation-contract.v1"

DOMAIN_AUTHORIZATION = "AIPotluck/spend-authorization/v1"
DOMAIN_PERMIT = "AIPotluck/dispatch-permit/v1"
DOMAIN_ACCEPTANCE = "AIPotluck/provider-acceptance/v1"
DOMAIN_USAGE = "AIPotluck/usage-report/v1"
DOMAIN_SETTLEMENT = "AIPotluck/settlement/v1"
DOMAIN_CONTRACT = "AIPotluck/allocation-contract/v1"

BillingBasis = Literal["reported_native_usage", "reproducible_text_usage"]
VerificationPolicy = Literal["node_reported", "platform_observed", "upstream_proven",
                             "attested_meter"]
AuthMethod = Literal["consumer_signed", "gateway_authenticated"]

#: Domain prefix for the per-request HTTP signature (`Authorization:
#: AIPotluck-Sig ...`). A request signature is not a settlement record, but it is
#: a signature all the same, so it lives beside the record domains and gets its
#: own prefix: a signature made to authenticate one HTTP call can never be
#: replayed as a spend authorization or a receipt.
DOMAIN_REQUEST_AUTH = "AIPotluck/request-auth/v1"

#: The authorization scheme of the per-request signature header.
REQUEST_AUTH_SCHEME = "AIPotluck-Sig"


def request_body_digest(body: bytes | str | None) -> str:
    """Return the canonical digest a request signature covers, for any body.

    Why a helper rather than hashing the raw bytes: the client and the server
    must agree on a value even when one side reformatted the JSON (added
    whitespace, reordered keys). Digesting the *strictly parsed* body means both
    sides recompute the digest over the same RFC 8785 canonical bytes, so a
    transport that normalizes JSON cannot silently break authentication.

    The empty-body case is defined rather than special-cased: a request with no
    body (every ``GET``, and a ``POST`` with zero bytes) digests
    ``canonical_digest(None)`` — the digest of the canonical encoding of JSON
    ``null``. A client with no body therefore signs exactly that value; there is
    no second convention to remember.

    Args:
        body: The raw request body, or `None`/empty when there is none.

    Returns:
        ``sha256:...`` digest of the canonical encoding of the parsed body, or
        of JSON ``null`` when the body is empty.

    Raises:
        DuplicateKeyError: If the body repeats a JSON key.
        NonCanonicalNumberError: If the body carries a float or an out-of-range
            integer.
        CanonicalizationError: If the body is not valid UTF-8 JSON.
    """
    if body is None:
        return canonical_digest(None)
    text = body.decode("utf-8") if isinstance(body, bytes) else body
    if not text.strip():
        return canonical_digest(None)
    return canonical_digest(loads_strict(text))


def request_auth_payload(
    *,
    method: str,
    path: str,
    query: str,
    body_digest: str,
    key_id: str,
    issued_at: dt.datetime,
    nonce: str,
) -> dict[str, Any]:
    """Return the canonical payload a request signature is taken over.

    Signing a value derived from the whole request rather than only the body is
    what stops a captured signature from being re-pointed at another path: the
    method, path, query and body digest are all covered, so a signature for
    ``POST /v1/requests`` cannot authorize a different call.

    Args:
        method: The HTTP method, upper-case.
        path: The request-target path, exactly as it appears before ``?``.
        query: The raw query string after ``?``, empty when absent.
        body_digest: The value from `request_body_digest`.
        key_id: The signing key's registry id.
        issued_at: When the client produced the signature.
        nonce: A single-use value for this key.

    Returns:
        The payload to canonicalize and sign.
    """
    return {
        "method": method,
        "path": path,
        "query": query,
        "body_digest": body_digest,
        "key_id": key_id,
        "issued_at": _aware(issued_at).isoformat(),
        "nonce": nonce,
    }


def format_request_auth_header(
    *, key_id: str, issued_at: dt.datetime, nonce: str, signature_b64: str
) -> str:
    """Render the ``AIPotluck-Sig`` header value for a signed request.

    The field order is ``key_id:issued_at:nonce:signature``. `issued_at` is an
    ISO 8601 instant and therefore contains colons, so the reader cannot split
    on every colon; see `parse_request_auth_header` for how it is recovered.

    Args:
        key_id: The signing key's registry id. Must not contain ``:``.
        issued_at: When the signature was produced.
        nonce: The single-use value that was signed.
        signature_b64: The base64 signature.

    Returns:
        The header value, without the scheme.

    Raises:
        ValueError: If `key_id` contains ``:``, which would make the header
            ambiguous to parse.
    """
    if ":" in key_id:
        raise ValueError("a key_id may not contain ':'")
    return (
        f"{key_id}:{_aware(issued_at).isoformat()}:{nonce}:{signature_b64}"
    )


def parse_request_auth_header(
    header: str | None,
) -> tuple[str, dt.datetime, str, str] | None:
    """Parse an ``Authorization: AIPotluck-Sig`` header value.

    Parsing rule, and why it is not a plain split: the header is
    ``key_id:issued_at:nonce:signature`` but the ISO 8601 instant itself carries
    colons. `key_id` never contains ``:`` (enforced at format time) and neither
    `nonce` (a URL-safe token) nor `signature` (base64) does, so the key id is
    the first field, the signature and nonce are the last two, and the instant
    is everything between them.

    Args:
        header: The raw header value, or `None` when absent.

    Returns:
        ``(key_id, issued_at, nonce, signature_b64)``, or `None` when the header
        is absent, uses another scheme, or is malformed. A malformed header is
        never partially accepted.
    """
    if header is None:
        return None
    scheme, separator, value = header.partition(" ")
    if not separator or scheme != REQUEST_AUTH_SCHEME:
        return None
    parts = value.strip().split(":")
    if len(parts) < 4:
        return None
    key_id, signature_b64, nonce = parts[0], parts[-1], parts[-2]
    issued_text = ":".join(parts[1:-2])
    if not key_id or ":" in key_id or not nonce or not signature_b64:
        return None
    try:
        issued_at = dt.datetime.fromisoformat(issued_text)
    except ValueError:
        return None
    if issued_at.tzinfo is None:
        return None
    return key_id, issued_at.astimezone(dt.UTC), nonce, signature_b64


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _aware(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(dt.UTC)


def _decimal(value: int) -> str:
    """Render an integer amount as a canonical decimal string.

    Amounts inside a signed payload may be negative (a payer's debit), while
    budget caps and billable totals are validated as non-negative at
    construction. Both use this canonical form so no float ever appears.
    """
    return str(int(value))


def _non_negative_decimal(value: int, field_name: str) -> str:
    """Render a non-negative integer amount, rejecting negatives.

    Args:
        value: The amount.
        field_name: Used in the error message.

    Returns:
        The canonical decimal string.

    Raises:
        ValueError: If the amount is negative.
    """
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative, got {value}")
    return str(int(value))


@dataclass(frozen=True)
class SignedRecord:
    """A payload plus the key that signed it and the resulting digest."""

    schema: str
    record_id: str
    revision: int
    signed_at: dt.datetime
    signer_key_id: str
    signer_id: str
    signature: str
    payload: Mapping[str, Any]
    previous_record_hash: str | None = None

    @property
    def hash(self) -> str:
        """Return the canonical digest of the payload."""
        return canonical_digest(dict(self.payload))


@dataclass(frozen=True)
class SpendAuthorization:
    """Consumer pre-authorization: budget, rate, evidence policy, and scope.

    Signed before dispatch so a later bill can be checked against agreed terms
    rather than a post-hoc argument. `gateway_authenticated` marks an old client
    that never signed; the UI must show it as gateway-authenticated rather than
    pretending it carries a consumer signature.
    """

    authorization_id: str
    payer_account_id: str
    consumer_id: str
    request_id: str
    request_digest: str
    max_charge_milli_credits: int
    pricing_version: str
    billing_basis: BillingBasis
    minimum_evidence_level: VerificationPolicy
    allowed_models: frozenset[str]
    allowed_trust_pools: frozenset[str]
    max_attempts: int
    duplicate_execution_policy: str
    max_provider_fanout: int
    nonce: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    auth_method: AuthMethod = "consumer_signed"
    key_id: str = ""
    delegating_key_id: str | None = None

    def __post_init__(self) -> None:
        """Validate the budget and window.

        Raises:
            ValueError: If the budget, attempt cap, fanout, or expiry window is invalid.
        """
        if self.max_charge_milli_credits <= 0:
            raise ValueError("max_charge_milli_credits must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.max_provider_fanout < 1:
            raise ValueError("max_provider_fanout must be >= 1")
        if _aware(self.expires_at) <= _aware(self.issued_at):
            raise ValueError("expires_at must be after issued_at")
        if not self.allowed_trust_pools:
            raise ValueError("allowed_trust_pools must not be empty")
        if self.auth_method == "gateway_authenticated" and not self.key_id:
            raise ValueError("gateway-authenticated authorizations must record the gateway key_id")

    def is_valid_at(self, moment: dt.datetime) -> bool:
        """Report whether the authorization is still inside its window."""
        return _aware(self.issued_at) <= _aware(moment) < _aware(self.expires_at)

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical signed payload."""
        return {
            "schema": SCHEMA_AUTHORIZATION,
            "authorization_id": self.authorization_id,
            "payer_account_id": self.payer_account_id,
            "consumer_id": self.consumer_id,
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "max_charge_milli_credits": _non_negative_decimal(
                self.max_charge_milli_credits, "max_charge_milli_credits"
            ),
            "pricing_version": self.pricing_version,
            "billing_basis": self.billing_basis,
            "minimum_evidence_level": self.minimum_evidence_level,
            "allowed_models": sorted(self.allowed_models),
            "allowed_trust_pools": sorted(self.allowed_trust_pools),
            "max_attempts": self.max_attempts,
            "duplicate_execution_policy": self.duplicate_execution_policy,
            "max_provider_fanout": self.max_provider_fanout,
            "auth_method": self.auth_method,
            "nonce": self.nonce,
            "issued_at": _aware(self.issued_at).isoformat(),
            "expires_at": _aware(self.expires_at).isoformat(),
        }

    def sign(self, private_key: Any, key_id: str) -> SignedRecord:
        """Sign the authorization, producing the record a verifier replays.

        This is the only signing path, and it serves delegated keys too: the
        signature covers the same canonical payload either way, because what
        distinguishes a delegated key is not a different payload but the scope
        check that `IdentityRegistry.verify_delegated` applies at verification
        time against the delegation covering `key_id`. Recording the delegation
        in the payload instead would only move the check to issue time, where a
        leaked key could authorize an unbounded spend afterwards.

        Args:
            private_key: The consumer's or delegatee's Ed25519 private key.
            key_id: The registry key ID that signed, consumer or delegatee.

        Returns:
            The signed authorization record.
        """
        payload = self.to_payload()
        return SignedRecord(
            schema=SCHEMA_AUTHORIZATION,
            record_id=self.authorization_id,
            revision=1,
            signed_at=_aware(self.issued_at),
            signer_key_id=key_id,
            signer_id=self.consumer_id,
            signature=sign_payload(DOMAIN_AUTHORIZATION, payload, private_key),
            payload=payload,
        )


def new_nonce() -> str:
    """Return a high-entropy nonce for authorizations and receipts."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class DispatchPermit:
    """Coordinator permit binding an authorization to one attempt.

    The permit caps exposure and names the allocation contract snapshot. The
    contributor still checks its own local policy afterwards: a valid platform
    signature never raises a local limit.
    """

    permit_id: str
    authorization_id: str
    authorization_hash: str
    attempt_id: str
    dispatch_epoch: int
    resource_id: str
    provider_id: str
    allocation_contract_hash: str
    max_exposed_milli_credits: int
    request_digest: str
    policy_version: int
    issued_at: dt.datetime
    lease_expires_at: dt.datetime
    platform_id: str
    key_id: str = ""

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical signed payload."""
        return {
            "schema": SCHEMA_PERMIT,
            "permit_id": self.permit_id,
            "platform_id": self.platform_id,
            "authorization_id": self.authorization_id,
            "authorization_hash": self.authorization_hash,
            "attempt_id": self.attempt_id,
            "dispatch_epoch": self.dispatch_epoch,
            "resource_id": self.resource_id,
            "provider_id": self.provider_id,
            "allocation_contract_hash": self.allocation_contract_hash,
            "max_exposed_milli_credits": _non_negative_decimal(
                self.max_exposed_milli_credits, "max_exposed_milli_credits"
            ),
            "request_digest": self.request_digest,
            "policy_version": self.policy_version,
            "issued_at": _aware(self.issued_at).isoformat(),
            "lease_expires_at": _aware(self.lease_expires_at).isoformat(),
        }

    def sign(self, private_key: Any, key_id: str) -> SignedRecord:
        """Sign the permit with the platform key."""
        payload = self.to_payload()
        return SignedRecord(
            schema=SCHEMA_PERMIT,
            record_id=self.permit_id,
            revision=1,
            signed_at=_aware(self.issued_at),
            signer_key_id=key_id,
            signer_id=self.platform_id,
            signature=sign_payload(DOMAIN_PERMIT, payload, private_key),
            payload=payload,
        )


@dataclass(frozen=True)
class ProviderAcceptance:
    """Contributor's signed confirmation after its own local checks pass."""

    acceptance_id: str
    permit_id: str
    permit_hash: str
    attempt_id: str
    provider_id: str
    device_id: str
    local_policy_version: int
    local_reserved_milli_credits: int
    accepted_at: dt.datetime

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical signed payload."""
        return {
            "schema": SCHEMA_ACCEPTANCE,
            "acceptance_id": self.acceptance_id,
            "permit_id": self.permit_id,
            "permit_hash": self.permit_hash,
            "attempt_id": self.attempt_id,
            "provider_id": self.provider_id,
            "device_id": self.device_id,
            "local_policy_version": self.local_policy_version,
            "local_reserved_milli_credits": _non_negative_decimal(
                self.local_reserved_milli_credits, "local_reserved_milli_credits"
            ),
            "accepted_at": _aware(self.accepted_at).isoformat(),
        }

    def sign(self, private_key: Any, key_id: str) -> SignedRecord:
        """Sign acceptance with the contribution device key."""
        payload = self.to_payload()
        return SignedRecord(
            schema=SCHEMA_ACCEPTANCE,
            record_id=self.acceptance_id,
            revision=1,
            signed_at=_aware(self.accepted_at),
            signer_key_id=key_id,
            signer_id=self.device_id,
            signature=sign_payload(DOMAIN_ACCEPTANCE, payload, private_key),
            payload=payload,
        )


@dataclass(frozen=True)
class UsageReport:
    """Device-signed measurement report.

    Signing proves who reported the numbers and that they were not edited in
    transit. It does not prove the numbers are true; that is what
    `verification_level` records, and why a signed report is still only
    `node_reported` unless an upstream source proves otherwise.

    The signing identity is the device, because the device holds the key.
    `provider_id` names the account the earnings belong to, and a device may
    only sign for the provider it is registered under.
    """

    report_id: str
    permit_id: str
    permit_hash: str
    acceptance_hash: str
    attempt_id: str
    provider_id: str
    device_id: str
    measurement: Measurement
    result_commitment: str | None
    final_event_seq: int
    outcome: Literal["completed", "failed", "cancelled", "outcome_unknown"]
    reported_at: dt.datetime
    revision: int = 1
    previous_report_hash: str | None = None
    key_id: str = ""

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical signed payload."""
        payload: dict[str, Any] = {
            "schema": SCHEMA_USAGE,
            "report_id": self.report_id,
            "permit_id": self.permit_id,
            "permit_hash": self.permit_hash,
            "acceptance_hash": self.acceptance_hash,
            "attempt_id": self.attempt_id,
            "provider_id": self.provider_id,
            "device_id": self.device_id,
            "measurement": self.measurement.to_payload(),
            "result_commitment": self.result_commitment,
            "final_event_seq": self.final_event_seq,
            "outcome": self.outcome,
            "revision": self.revision,
            "previous_report_hash": self.previous_report_hash,
            "reported_at": _aware(self.reported_at).isoformat(),
        }
        return payload

    def sign(self, private_key: Any, key_id: str) -> SignedRecord:
        """Sign the report with the device key."""
        payload = self.to_payload()
        return SignedRecord(
            schema=SCHEMA_USAGE,
            record_id=self.report_id,
            revision=self.revision,
            signed_at=_aware(self.reported_at),
            signer_key_id=key_id,
            signer_id=self.device_id,
            signature=sign_payload(DOMAIN_USAGE, payload, private_key),
            payload=payload,
            previous_record_hash=self.previous_report_hash,
        )

    def evidence_claim(self) -> frozenset[str]:
        """Return what this report's evidence level actually establishes."""
        return EVIDENCE_SCOPE[self.measurement.usage.verification_level]


@dataclass(frozen=True)
class SettlementReceipt:
    """Platform-signed receipt, issued only after the ledger transaction commits.

    `postings` must balance to zero in a single currency. The nonce is high
    entropy so the public transparency log carries no correlation between a
    consumer's requests.
    """

    receipt_id: str
    platform_id: str
    journal_id: str
    settlement_revision: int
    authorization_hash: str
    request_id: str
    service_unit_id: str
    winner_attempt_id: str
    billing_basis: BillingBasis
    verification_level: VerificationPolicy
    pricing_version: str
    allocation_contract_hash: str
    billable_tokens: int
    postings: tuple[tuple[str, int], ...]
    result_commitment: str | None
    previous_receipt_hash: str | None
    nonce: str
    settled_at: dt.datetime
    key_id: str = ""

    def __post_init__(self) -> None:
        """Verify the postings balance before a receipt can exist.

        Raises:
            VerificationError: If the postings do not sum to zero.
        """
        if sum(delta for _, delta in self.postings) != 0:
            raise VerificationError(
                f"settlement {self.receipt_id} postings do not sum to zero: {self.postings}"
            )
        if not self.nonce or len(self.nonce) < 22:
            raise VerificationError("receipt nonce must be high entropy (>= 22 chars)")

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical signed payload."""
        return {
            "schema": SCHEMA_SETTLEMENT,
            "platform_id": self.platform_id,
            "receipt_id": self.receipt_id,
            "key_id": self.key_id,
            "journal_id": self.journal_id,
            "settlement_revision": str(self.settlement_revision),
            "previous_receipt_hash": self.previous_receipt_hash,
            "authorization_hash": self.authorization_hash,
            "request_id": self.request_id,
            "service_unit_id": self.service_unit_id,
            "winner_attempt_id": self.winner_attempt_id,
            "billing_basis": self.billing_basis,
            "verification_level": self.verification_level,
            "pricing_version": self.pricing_version,
            "allocation_contract_hash": self.allocation_contract_hash,
            "billable_tokens": _decimal(self.billable_tokens),
            "currency": "milli-credit",
            "postings": [
                {"account": account, "delta": _decimal(delta)}
                for account, delta in self.postings
            ],
            "result_commitment": self.result_commitment,
            "receipt_nonce": self.nonce,
            "settled_at": _aware(self.settled_at).isoformat(),
        }

    def sign(self, private_key: Any, key_id: str) -> SignedRecord:
        """Sign the receipt with the platform settlement key."""
        payload = self.to_payload()
        return SignedRecord(
            schema=SCHEMA_SETTLEMENT,
            record_id=self.receipt_id,
            revision=self.settlement_revision,
            signed_at=_aware(self.settled_at),
            signer_key_id=key_id,
            signer_id=self.platform_id,
            signature=sign_payload(DOMAIN_SETTLEMENT, payload, private_key),
            payload=payload,
            previous_record_hash=self.previous_receipt_hash,
        )


class RecordStore:
    """Keeps the current revision per record ID and freezes any conflict.

    When the same `(record_id, revision)` arrives with different content, both
    versions are retained and the entry is frozen. The store never resolves the
    conflict by keeping the last writer.
    """

    def __init__(self) -> None:
        self._current: dict[tuple[str, str], SignedRecord] = {}
        self._frozen: dict[tuple[str, int], list[SignedRecord]] = {}

    def submit(self, record: SignedRecord) -> str:
        """Store a record, applying revision and conflict rules.

        Args:
            record: The signed record to store.

        Returns:
            One of `applied`, `replaced`, `ignored_duplicate`,
            `ignored_stale`, or `conflict_frozen`.

        Raises:
            ConflictError: If a revision skips a required predecessor chain.
        """
        if record.revision > 1 and record.previous_record_hash is None:
            raise ConflictError(
                f"record {record.record_id} revision {record.revision} must reference "
                "a previous record hash"
            )
        key = (record.schema, record.record_id)
        current = self._current.get(key)
        if current is None:
            if record.revision != 1:
                raise ConflictError(
                    f"record {record.record_id} arrived at revision {record.revision} "
                    "with no revision 1 on file"
                )
            self._current[key] = record
            return "applied"
        if current.hash == record.hash and current.revision == record.revision:
            return "ignored_duplicate"
        if record.revision <= current.revision:
            if record.hash != current.hash:
                self._frozen.setdefault((record.record_id, record.revision), []).extend(
                    [current, record]
                )
                return "conflict_frozen"
            return "ignored_stale"
        if record.previous_record_hash != current.hash:
            raise ConflictError(
                f"record {record.record_id} revision {record.revision} references "
                f"{record.previous_record_hash} but current is {current.hash}"
            )
        self._current[key] = record
        return "replaced"

    def get(self, schema: str, record_id: str) -> SignedRecord | None:
        """Return the current revision of a record."""
        return self._current.get((schema, record_id))

    def frozen_conflicts(self) -> dict[tuple[str, int], list[SignedRecord]]:
        """Return all frozen conflicts."""
        return dict(self._frozen)

    def all_records(self) -> list[SignedRecord]:
        """Return every current record."""
        return list(self._current.values())


def verify_chain(
    registry: IdentityRegistry,
    record: SignedRecord,
    purpose: str,
    owner_id: str | None = None,
    domain: str | None = None,
    witnessed_at: dt.datetime | None = None,
) -> None:
    """Verify a signed record's signature against the identity registry.

    Args:
        registry: The trusted key registry.
        record: The record to verify.
        purpose: Required signing role.
        owner_id: Required identity; defaults to the record's signer.
        domain: Domain prefix; defaults to the record schema's mapping.
        witnessed_at: When the platform first accepted this message. Pass it for
            *admission* decisions so key validity is judged against a
            platform-controlled instant rather than the record's self-reported
            one. A record cannot make itself acceptable by claiming to have been
            signed before its key was revoked, because the claimed time is
            written by whoever produced the record. Omit it for historical
            verification of a record already accepted, where the self-reported
            time is the right thing to check.

    Raises:
        VerificationError: If the domain is unknown for the schema.
        SignatureError: If key binding, revocation, or signature checks fail.
    """
    resolved_domain = domain or _DOMAIN_BY_SCHEMA.get(record.schema)
    if resolved_domain is None:
        raise VerificationError(f"no domain prefix registered for schema {record.schema}")
    registry.verify(
        key_id=record.signer_key_id,
        owner_id=owner_id or record.signer_id,
        purpose=purpose,  # type: ignore[arg-type]
        domain=resolved_domain,
        payload=dict(record.payload),
        signature_b64=record.signature,
        signed_at=witnessed_at or record.signed_at,
    )


_DOMAIN_BY_SCHEMA: dict[str, str] = {
    SCHEMA_AUTHORIZATION: DOMAIN_AUTHORIZATION,
    SCHEMA_PERMIT: DOMAIN_PERMIT,
    SCHEMA_ACCEPTANCE: DOMAIN_ACCEPTANCE,
    SCHEMA_USAGE: DOMAIN_USAGE,
    SCHEMA_SETTLEMENT: DOMAIN_SETTLEMENT,
    SCHEMA_CONTRACT: DOMAIN_CONTRACT,
}


def verify_signature_standalone(
    registry: IdentityRegistry,
    record: SignedRecord,
    purpose: str,
    owner_id: str | None = None,
) -> bool:
    """Return whether a record's signature verifies, without raising on failure."""
    try:
        verify_chain(registry, record, purpose, owner_id)
    except Exception:
        return False
    return True


def self_check_signature(record: SignedRecord, public_key: Any) -> bool:
    """Verify a record against a raw public key, ignoring registry bindings."""
    domain = _DOMAIN_BY_SCHEMA.get(record.schema)
    if domain is None:
        raise VerificationError(f"no domain prefix registered for schema {record.schema}")
    return verify_payload(domain, dict(record.payload), record.signature, public_key)


@dataclass
class ReceiptChain:
    """Links receipts by hash so a party can prove their receipt sequence."""

    receipts: list[SignedRecord] = field(default_factory=list)

    def append(self, receipt: SignedRecord) -> None:
        """Append a receipt, requiring it to reference the previous one.

        Raises:
            ConflictError: If the new receipt does not chain to the last one.
        """
        if self.receipts:
            expected = self.receipts[-1].hash
            if receipt.previous_record_hash != expected:
                raise ConflictError(
                    f"receipt {receipt.record_id} references "
                    f"{receipt.previous_record_hash}, expected {expected}"
                )
        self.receipts.append(receipt)

    def digests(self) -> list[str]:
        """Return every receipt digest in order."""
        return [receipt.hash for receipt in self.receipts]


def signed_record_from_dict(body: dict[str, Any]) -> SignedRecord:
    """Rebuild a signed record from its serialized form.

    Raises:
        GatewayError: If required fields are missing.
    """
    try:
        return SignedRecord(
            schema=str(body["schema"]),
            record_id=str(body["record_id"]),
            revision=int(body["revision"]),
            signed_at=dt.datetime.fromisoformat(str(body["signed_at"])),
            signer_key_id=str(body["signer_key_id"]),
            signer_id=str(body["signer_id"]),
            signature=str(body["signature"]),
            payload=dict(body["payload"]),
            previous_record_hash=body.get("previous_record_hash"),
        )
    except KeyError as exc:
        raise GatewayError(f"serialized record is missing {exc}") from exc
    except ValueError as exc:
        raise GatewayError(f"serialized record is malformed: {exc}") from exc
