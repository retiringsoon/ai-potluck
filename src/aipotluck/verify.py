"""Offline verification: recompute a party's own records independently.

The verifier never returns a single "verified" boolean. It reports five
dimensions separately, because they fail independently and a user who sees one
green light would otherwise conclude that real model consumption was proven:

- `signature_valid`      the records are signed by keys bound to the right identities
- `metering_provenance`  what evidence level the usage actually carries
- `allocation_valid`     beneficiaries match the pre-agreed contract
- `ledger_included`      the receipt is committed and included in a log whose root
                         the verifier already trusts
- `account_history_complete` the cached sequence has no gaps

Recomputation uses only the party's own cached records plus the contracts and
keys it already trusts. It does not query the platform, because a platform that
is withholding records must be detectable rather than papered over.

Inclusion is judged against a trusted root, never against the root the supplied
proof claims. A proof is attacker-supplied data: an attacker who fabricates a
one-leaf log can produce a proof that folds perfectly to its own fabricated root.
Such a log proves only that the attacker's own hashes agree with each other, so
without an independently held root or checkpoint the dimension reports `unknown`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from .allocation import AllocationContract
from .canonical import canonical_digest
from .errors import VerificationError
from .identity import IdentityRegistry
from .ledger import Journal
from .metering import EVIDENCE_SCOPE
from .records import (
    SCHEMA_ACCEPTANCE,
    SCHEMA_AUTHORIZATION,
    SCHEMA_PERMIT,
    SCHEMA_SETTLEMENT,
    SCHEMA_USAGE,
    SignedRecord,
    verify_chain,
)
from .transparency import Checkpoint, InclusionProof, verify_inclusion

Verdict = Literal["pass", "fail", "unknown", "not_applicable"]


@dataclass
class VerificationReport:
    """Per-dimension verdicts plus the anomalies behind them."""

    signature_valid: Verdict = "unknown"
    metering_provenance: Verdict = "unknown"
    allocation_valid: Verdict = "unknown"
    ledger_included: Verdict = "unknown"
    account_history_complete: Verdict = "unknown"
    anomalies: list[str] = field(default_factory=list)
    evidence_levels: dict[str, int] = field(default_factory=dict)
    recomputed_earnings: dict[str, int] = field(default_factory=dict)
    recomputed_spend: dict[str, int] = field(default_factory=dict)
    total_receipts: int = 0
    verified_at: dt.datetime = field(
        default_factory=lambda: dt.datetime.now(dt.UTC)
    )

    @property
    def all_pass(self) -> bool:
        """Report whether every dimension passed.

        This is a convenience summary. It must never be shown as proof of real
        model consumption; `metering_provenance` is the dimension that carries
        that distinction.
        """
        return all(
            verdict in {"pass", "not_applicable"}
            for verdict in (
                self.signature_valid,
                self.metering_provenance,
                self.allocation_valid,
                self.ledger_included,
                self.account_history_complete,
            )
        )

    def render(self) -> str:
        """Render the report as an operator-facing multi-line summary.

        `all_pass` is printed last among the verdict lines and labelled as a
        summary, never as a sixth dimension: it is True whenever nothing
        contradicted the records, which includes a dimension that stayed
        ``unknown`` for want of evidence. Reading it as proof of real model
        consumption is exactly the mistake the five separate verdicts exist to
        prevent, so the line that carries that distinction —
        `metering_provenance` — is printed above it and its anomaly below.
        """
        lines = [
            f"signature_valid           : {self.signature_valid}",
            f"metering_provenance       : {self.metering_provenance}",
            f"allocation_valid          : {self.allocation_valid}",
            f"ledger_included           : {self.ledger_included}",
            f"account_history_complete  : {self.account_history_complete}",
            f"receipts_checked          : {self.total_receipts}",
            f"all_pass (summary)        : {str(self.all_pass).lower()}",
        ]
        for level, count in sorted(self.evidence_levels.items()):
            lines.append(f"  evidence {level:<20}: {count} record(s)")
        for account, amount in sorted(self.recomputed_earnings.items()):
            lines.append(f"  earned {account:<21}: {amount} milli-credits")
        for account, amount in sorted(self.recomputed_spend.items()):
            lines.append(f"  spent  {account:<21}: {amount} milli-credits")
        for anomaly in self.anomalies:
            lines.append(f"  ! {anomaly}")
        return "\n".join(lines)


@dataclass
class VerificationInput:
    """Everything a party holds locally, needed for offline verification.

    `inclusion_proofs` and `leaf_bytes` are attacker-supplied: they arrive with
    the data under test, so nothing in them can be taken on trust. `trusted_root`
    is the verifier's own already-held view of the log's root, and
    `trusted_checkpoint` is a signed statement of that root which
    `checkpoint_public_key` can authenticate. At least one of the two must be
    supplied for `ledger_included` to reach `pass`.
    """

    registry: IdentityRegistry
    authorizations: Sequence[SignedRecord] = ()
    permits: Sequence[SignedRecord] = ()
    acceptances: Sequence[SignedRecord] = ()
    usage_reports: Sequence[SignedRecord] = ()
    receipts: Sequence[SignedRecord] = ()
    journals: Sequence[Journal] = ()
    contracts: Mapping[str, AllocationContract] = field(default_factory=dict)
    inclusion_proofs: Mapping[str, InclusionProof] = field(default_factory=dict)
    leaf_bytes: Mapping[str, bytes] = field(default_factory=dict)
    trusted_root: str | None = None
    trusted_checkpoint: Checkpoint | None = None
    checkpoint_public_key: Any | None = None
    expected_receipt_ids: Sequence[str] = ()
    observed_sequence: Sequence[int] = ()
    expected_sequence: Sequence[int] = ()
    require_upstream_proof: bool = False


def verify_offline(data: VerificationInput) -> VerificationReport:
    """Recompute a party's records and report each verification dimension.

    Args:
        data: The party's locally held records, keys, and proofs.

    Returns:
        A report with a verdict per dimension and every anomaly found.

    Raises:
        VerificationError: Never raised for missing data; missing evidence
            produces an `unknown` verdict so the caller can distinguish "no
            evidence" from "contradictory evidence".
    """
    report = VerificationReport(total_receipts=len(data.receipts))

    # -- signatures -------------------------------------------------------
    signature_anomalies, signature_ok = _verify_signatures(data)
    report.anomalies.extend(signature_anomalies)
    report.signature_valid = "pass" if signature_ok and signature_anomalies == [] else (
        "fail" if signature_anomalies else "unknown"
    )

    # -- metering provenance --------------------------------------------
    levels = _evidence_profile(data.usage_reports)
    report.evidence_levels = levels
    highest = _highest_evidence_level(levels)
    if not levels:
        report.metering_provenance = "unknown"
        report.anomalies.append("no usage reports held; provenance cannot be assessed")
    elif data.require_upstream_proof and highest not in {"upstream_proven", "attested_meter"}:
        report.metering_provenance = "fail"
        report.anomalies.append(
            f"policy requires upstream_proven or attested_meter, highest held is {highest}"
        )
    else:
        report.metering_provenance = "pass"
        report.anomalies.append(
            f"highest evidence level is {highest}; a signed report proves who reported "
            "the numbers, not that the upstream consumed them"
        )

    # -- allocation ------------------------------------------------------
    allocation_anomalies, earnings, spend = _recompute_allocation(data)
    report.anomalies.extend(allocation_anomalies)
    report.recomputed_earnings = earnings
    report.recomputed_spend = spend
    report.allocation_valid = "fail" if allocation_anomalies else "pass"

    # -- ledger inclusion -------------------------------------------------
    # `_verify_inclusion` returns the verdict directly: it distinguishes "the
    # proof contradicts a root I trust" (fail) from "I hold no root to judge it
    # against" (unknown), which a bare anomaly count cannot express.
    inclusion_anomalies, report.ledger_included = _verify_inclusion(data)
    report.anomalies.extend(inclusion_anomalies)

    # -- account history --------------------------------------------------
    history_anomalies, history_ok = _verify_history(data)
    report.anomalies.extend(history_anomalies)
    report.account_history_complete = "pass" if history_ok and not history_anomalies else (
        "fail" if history_anomalies else "unknown"
    )

    return report


#: Which signing purpose an authorization's signature is checked under, keyed by
#: the authentication method the record itself declares. ``consumer_signed`` is
#: the consumer's own Ed25519 key: the party being charged signed the exact work
#: and ceiling. ``gateway_authenticated`` is the platform's settlement key, for a
#: request the server admitted on a credential it holds — an operator-issued API
#: key — instead of a signature the consumer produced; the authorization is then
#: the platform's own record of the terms it admitted, which is real evidence of
#: a different strength, and the receipt keeps the label rather than presenting
#: it as a consumer signature. A method this table does not name is reported as
#: an anomaly instead of being checked under a guessed purpose: a label no build
#: defined has no check it could honestly pass.
_AUTHORIZATION_PURPOSES: dict[str, str] = {
    "consumer_signed": "consumer",
    "gateway_authenticated": "platform_settlement",
}


def _verify_signatures(data: VerificationInput) -> tuple[list[str], bool]:
    anomalies: list[str] = []
    ok = True
    # An authorization is checked apart from the other record types because the
    # key that had to sign it depends on the authentication method the record
    # declares, rather than being one fixed purpose.
    for record in data.authorizations:
        if record.schema != SCHEMA_AUTHORIZATION:
            anomalies.append(
                f"record {record.record_id} claims schema {record.schema}, "
                f"expected {SCHEMA_AUTHORIZATION}"
            )
            ok = False
            continue
        method = str(record.payload.get("auth_method", "consumer_signed"))
        purpose = _AUTHORIZATION_PURPOSES.get(method)
        if purpose is None:
            anomalies.append(
                f"{SCHEMA_AUTHORIZATION} {record.record_id} declares auth_method "
                f"{method!r}, which this build defines no signing key for; an "
                "unknown method is reported rather than passed"
            )
            ok = False
            continue
        try:
            # A platform-signed authorization is verified against the identity
            # the registry binds the settlement key to, exactly as settlement
            # verified it when the credit moved; a consumer-signed one against
            # the consumer it names.
            owner_id = (
                data.registry.get_key(record.signer_key_id).owner_id
                if purpose == "platform_settlement"
                else record.signer_id
            )
            verify_chain(data.registry, record, purpose, owner_id=owner_id)
        except Exception as exc:
            anomalies.append(f"{SCHEMA_AUTHORIZATION} {record.record_id}: {exc}")
            ok = False
    checks = (
        (data.permits, SCHEMA_PERMIT, "platform_settlement"),
        (data.acceptances, SCHEMA_ACCEPTANCE, "provider_device"),
        (data.usage_reports, SCHEMA_USAGE, "provider_device"),
        (data.receipts, SCHEMA_SETTLEMENT, "platform_settlement"),
    )
    for records, schema, purpose in checks:
        for record in records:
            if record.schema != schema:
                anomalies.append(
                    f"record {record.record_id} claims schema {record.schema}, expected {schema}"
                )
                ok = False
                continue
            try:
                verify_chain(data.registry, record, purpose)
            except Exception as exc:
                anomalies.append(f"{schema} {record.record_id}: {exc}")
                ok = False
    return anomalies, ok


def _evidence_profile(usage_reports: Sequence[SignedRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for report in usage_reports:
        measurement = report.payload.get("measurement")
        if not isinstance(measurement, Mapping):
            continue
        usage = measurement.get("usage")
        if not isinstance(usage, Mapping):
            continue
        level = str(usage.get("verification_level", "unknown"))
        counts[level] = counts.get(level, 0) + 1
    return counts


#: Presentation and policy order. Design 22.2 is explicit that evidence levels
#: are not a ranking: a "higher" entry here does not necessarily cover more
#: fields than a lower one. This tuple serves exactly two purposes, neither of
#: which is a subsumption claim:
#:
#: 1. picking which level to display as "highest available"
#:    (`_highest_evidence_level`);
#: 2. evaluating whether a report satisfies a minimum the consumer stated in
#:    their authorization (`meets_evidence_policy`). That is a product decision
#:    the platform declares, not a theorem about the levels.
#:
#: To ask what a level actually establishes, use `evidence_covers`, which
#: consults its declared scope.
_EVIDENCE_ORDER = ("node_reported", "platform_observed", "upstream_proven", "attested_meter")


def meets_evidence_policy(actual: str, required: str) -> bool:
    """Report whether a measurement's evidence level satisfies a stated minimum.

    This is deliberately separate from `evidence_covers`. That function answers
    "what does this level establish?". This one answers "does the platform's
    stated policy accept it?" — a narrower, product-level decision. Neither
    implies that levels form a hierarchy of guarantees (design 22.2).

    Args:
        actual: The verification level recorded on the measurement.
        required: The minimum level the authorization demanded.

    Returns:
        True when `actual` sits at or above `required` under the declared policy
        order.

    Raises:
        VerificationError: If either level is not one of the four defined levels.
            An unrecognised level must not silently pass a policy check.
    """
    for level in (actual, required):
        if level not in _EVIDENCE_ORDER:
            raise VerificationError(
                f"unknown evidence level {level!r}; recognised levels are "
                f"{list(_EVIDENCE_ORDER)}"
            )
    return _EVIDENCE_ORDER.index(actual) >= _EVIDENCE_ORDER.index(required)


def _highest_evidence_level(levels: Mapping[str, int]) -> str:
    """Return the level to present as "highest available".

    The ordering in `_EVIDENCE_ORDER` is a display choice, not a ranking claim.
    Evidence levels are not a hierarchy (design 22.2): this function does not
    assert that the returned level's guarantees subsume those of any other
    level, only that it is the last one present under the presentation order.
    Use `evidence_covers` to test what a level establishes.

    Args:
        levels: Counts of records held per verification level.

    Returns:
        The level to display, or `"unknown"` when no levels are present.
    """
    present = [level for level in _EVIDENCE_ORDER if levels.get(level)]
    return present[-1] if present else "unknown"


def evidence_covers(level: str, claim: str) -> bool:
    """Report whether an evidence level establishes a specific claim.

    Args:
        level: The verification level on a measurement.
        claim: The claim to test, e.g. ``upstream_channel`` or
            ``metering_execution``.

    Returns:
        True when the level's declared scope includes the claim.

    Raises:
        KeyError: If the level is not one of the four defined levels.
    """
    return claim in EVIDENCE_SCOPE[level]


def _recompute_allocation(
    data: VerificationInput,
) -> tuple[list[str], dict[str, int], dict[str, int]]:
    anomalies: list[str] = []
    earnings: dict[str, int] = {}
    spend: dict[str, int] = {}

    for receipt in data.receipts:
        contract_hash = str(receipt.payload.get("allocation_contract_hash", ""))
        contract = data.contracts.get(contract_hash)
        if contract is None:
            anomalies.append(
                f"receipt {receipt.record_id} references allocation contract "
                f"{contract_hash or '<none>'} which is not held locally"
            )
            continue
        billable_raw = receipt.payload.get("billable_tokens", "0")
        billable = int(str(billable_raw))
        expected = contract.distribute(billable)
        postings = receipt.payload.get("postings")
        if not isinstance(postings, Sequence):
            anomalies.append(f"receipt {receipt.record_id} has no postings array")
            continue
        actual: dict[str, int] = {}
        for posting in postings:
            if not isinstance(posting, Mapping):
                continue
            delta = int(str(posting["delta"]))
            account = str(posting["account"])
            if delta < 0:
                payer = account.removesuffix("/reserved")
                spend[payer] = spend.get(payer, 0) + (-delta)
            else:
                beneficiary = account.removesuffix("/pending")
                actual[beneficiary] = actual.get(beneficiary, 0) + delta
        if actual != expected:
            anomalies.append(
                f"receipt {receipt.record_id} pays {actual} but contract "
                f"{contract.contract_id} v{contract.version} requires {expected}"
            )
            continue
        if canonical_digest(contract.digest_payload()) != contract_hash:
            anomalies.append(
                f"contract {contract.contract_id} digest does not match the hash "
                f"bound into receipt {receipt.record_id}"
            )
            continue
        for beneficiary, amount in expected.items():
            earnings[beneficiary] = earnings.get(beneficiary, 0) + amount

    for journal in data.journals:
        if journal.total_delta() != 0:
            anomalies.append(
                f"journal {journal.journal_id} sums to {journal.total_delta()}, expected 0"
            )
    return anomalies, earnings, spend


def _trusted_root(data: VerificationInput) -> tuple[str | None, list[str]]:
    """Resolve the log root this verifier already trusts.

    A supplied `trusted_checkpoint` is usable only once its signature verifies
    under `checkpoint_public_key`. An unauthenticated checkpoint is no better
    than the proofs it is meant to anchor, because it arrives over the same
    untrusted channel.

    Args:
        data: The verification input.

    Returns:
        The trusted root hash, or None when none could be established. The second
        element carries any anomalies found while resolving it.
    """
    anomalies: list[str] = []
    checkpoint = data.trusted_checkpoint
    if checkpoint is None:
        return data.trusted_root, anomalies
    if data.checkpoint_public_key is None:
        anomalies.append(
            f"checkpoint for epoch {checkpoint.checkpoint_epoch} was supplied without a "
            "checkpoint public key, so it cannot authenticate a root"
        )
        return None, anomalies
    try:
        checkpoint_ok = checkpoint.verify(data.checkpoint_public_key)
    except VerificationError as exc:
        # An unsigned or malformed checkpoint is attacker-supplied data. It must
        # degrade to "no trusted root", never propagate: verify_offline promises
        # missing evidence yields a verdict, not an exception.
        anomalies.append(
            f"checkpoint for epoch {checkpoint.checkpoint_epoch} is unusable: {exc}"
        )
        return None, anomalies
    if not checkpoint_ok:
        anomalies.append(
            f"checkpoint for epoch {checkpoint.checkpoint_epoch} does not verify under the "
            "supplied checkpoint public key"
        )
        return None, anomalies
    if data.trusted_root is not None and data.trusted_root != checkpoint.root_hash:
        anomalies.append(
            f"trusted root {data.trusted_root} disagrees with the signed checkpoint root "
            f"{checkpoint.root_hash}"
        )
        return None, anomalies
    return checkpoint.root_hash, anomalies


def _verify_inclusion(data: VerificationInput) -> tuple[list[str], Verdict]:
    """Judge log inclusion against a root the verifier already trusts.

    The supplied proofs are attacker-controlled, so a proof is never allowed to
    vouch for the root it names. Each proof is re-folded against the trusted root
    instead, which is what makes the verdict independent of the party under
    scrutiny.

    Args:
        data: The verification input.

    Returns:
        The anomalies found and the verdict for the `ledger_included` dimension.
        `unknown` means nothing here contradicts the claim but nothing
        independent confirms it either, which is the case whenever receipts are
        held without a trusted root to judge their proofs against.
    """
    anomalies: list[str] = []

    # Resolve the trusted root even when no receipts are held: an unusable
    # checkpoint is itself a finding, and silently skipping it would hide a
    # broken or forged checkpoint from the caller.
    root, root_anomalies = _trusted_root(data)
    anomalies.extend(root_anomalies)

    if not data.receipts:
        return anomalies, "unknown"

    # A receipt with no proof, or one whose proof misses the trusted root, is a
    # positive contradiction. A receipt carrying only an unanchored proof is not.
    contradicted = False

    for receipt in data.receipts:
        receipt_hash = receipt.hash
        proof = data.inclusion_proofs.get(receipt_hash)
        if proof is None:
            anomalies.append(
                f"receipt {receipt.record_id} has no inclusion proof; "
                "presence in the log is unverified"
            )
            contradicted = True
            continue
        leaf = data.leaf_bytes.get(receipt_hash)
        if leaf is None:
            anomalies.append(
                f"receipt {receipt.record_id} has a proof but the local leaf bytes are missing"
            )
            contradicted = True
            continue
        if root is None:
            # The audit path folds to whatever root the proof itself claims, and
            # that claim arrived with the untrusted data. Report the missing
            # anchor instead of treating self-consistency as proof of presence.
            anomalies.append(
                f"receipt {receipt.record_id} has an inclusion proof but no trusted log root "
                f"is held; the proof reaches {proof.root_hash}, which this verifier has not "
                "independently confirmed, so presence in the log is unverified"
            )
            continue
        anchored = replace(proof, root_hash=root)
        if not verify_inclusion(anchored, leaf):
            anomalies.append(
                f"inclusion proof for receipt {receipt.record_id} does not reach the "
                f"trusted root {root}"
            )
            contradicted = True

    for contract_hash in data.contracts:
        if not contract_hash.startswith("sha256:"):
            anomalies.append(f"contract hash {contract_hash!r} is not a canonical digest")
            contradicted = True

    if contradicted:
        return anomalies, "fail"
    if root is None:
        return anomalies, "unknown"
    return anomalies, "pass"


def _verify_history(data: VerificationInput) -> tuple[list[str], bool]:
    anomalies: list[str] = []
    if data.expected_receipt_ids:
        held = {record.record_id for record in data.receipts}
        missing = [rid for rid in data.expected_receipt_ids if rid not in held]
        if missing:
            anomalies.append(
                f"{len(missing)} receipt(s) previously promised are missing locally: "
                f"{sorted(missing)[:5]}"
            )
    if data.expected_sequence:
        expected = set(data.expected_sequence)
        observed = set(data.observed_sequence)
        gaps = sorted(expected - observed)
        if gaps:
            anomalies.append(
                f"account sequence has {len(gaps)} gap(s); the first missing value is "
                f"{gaps[0] if gaps else 'n/a'}"
            )
            return anomalies, False
        extras = sorted(observed - expected)
        if extras:
            anomalies.append(
                f"observed sequence values beyond the promised range: {extras[:5]}"
            )
            return anomalies, False
        if data.expected_sequence and max(expected) != max(observed):
            anomalies.append(
                f"latest sequence is {max(observed)} but {max(expected)} was promised"
            )
            return anomalies, False
    if not data.expected_sequence and not data.expected_receipt_ids:
        anomalies.append(
            "no promised sequence or receipt list was supplied; completeness is unassessed"
        )
    return anomalies, True