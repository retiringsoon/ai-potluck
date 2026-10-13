"""Two-sided reconciliation, consumer and contributor side.

The platform's trust claim is that nobody can quietly rewrite history, because
each party keeps its own record of the same event: the consumer holds a signed
receipt and the contributor holds what it signed and executed in ``node.db``.
Any one record can be lost or edited on its own. Making the disagreement
*visible* is what this module does. It never freezes an account, never writes a
row, and never resolves a difference — it reports the difference and names the
record, and a person decides what to do.

Two views live here, one per client party:

`verify_receipt_from_server`
    One receipt through `verify.verify_offline`'s five dimensions, read from the
    three documents a consumer can fetch over HTTP.

`reconcile_node`
    The contributor's view: local ``executions`` and ``spool`` rows against the
    attempts the server says it dispatched to this node.

The operator's views — `verify_receipt_at_server` over the server's own store
and `reconcile_server` across its tables — live in ``ai-potluck-server``, which
imports the shared verdict shapes (`Difference`, `ReceiptVerification`) and the
claimed-leaf derivation from here.

Every function here is read-only. A caller reading somebody else's database
opens it with `NodeStore.open_existing`, so a path that names nothing is
refused rather than created: an empty database holds no rows, and no rows
report no differences.

What the consumer's view cannot prove
------------------------------------
A consumer fetching over HTTP learns the platform's settlement public key from
the same server that serves the receipt, so `ledger_included` authenticates the
checkpoint against a key the server supplied. That detects a lost, reordered or
short log and a proof that does not fold; it does not detect a server that
rewrote history and re-signed the result. Pinning ``public_key_b64`` out of
band and comparing checkpoints across runs is what closes that gap.
`metering_provenance` stays ``unknown`` on this path because the consumer holds
no signed usage report. That is the honest answer, and it is why the summary
never presents `all_pass` as proof the upstream really consumed the tokens.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from .allocation import AllocationContract
from .canonical import base64_decode, canonical_digest
from .deploy import PROVISIONED_AT
from .errors import PotluckError, ProtocolError, VerificationError
from .identity import IdentityRegistry
from .persistence import ExecutionRecord, ReceiptRecord, SpoolRecord
from .records import SignedRecord, signed_record_from_dict
from .transparency import Checkpoint, InclusionProof, LogEntry
from .verify import VerificationInput, VerificationReport, verify_offline

#: Where a verified receipt's facts came from: the operator's own store, or the
#: documents a consumer fetched over HTTP. The distinction is part of the
#: verdict, because the two paths do not hold the same evidence.
Source = Literal["server-store", "server-http"]

#: The five dimensions, in the order `VerificationReport.render` shows them.
DIMENSIONS: tuple[str, ...] = (
    "signature_valid",
    "metering_provenance",
    "allocation_valid",
    "ledger_included",
    "account_history_complete",
)

#: The attempt states that mean the server will never do more work on an attempt.
_TERMINAL_ATTEMPT_STATUSES = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED", "OUTCOME_UNKNOWN", "REVOKED_BEFORE_SUBMIT"}
)


@dataclass(frozen=True)
class Difference:
    """One disagreement about one record, named so a person can act on it.

    `subject` always identifies the record — an attempt, a receipt, a log
    position — because "the books disagree" is not actionable and "receipt R-7
    has no log leaf" is. `local` and `remote` carry only the fields the
    difference is about, never a whole payload: a rendered difference goes to a
    terminal and into a ticket, and a receipt body carries account balances.
    """

    subject: str
    kind: str
    local: str
    remote: str

    def to_payload(self) -> dict[str, str]:
        """Return the difference as a JSON-ready mapping."""
        return {
            "subject": self.subject,
            "kind": self.kind,
            "local": self.local,
            "remote": self.remote,
        }


@dataclass(frozen=True)
class ReceiptVerification:
    """One receipt's five-dimension verdict, plus where the facts came from.

    `findings` carries what a `VerificationReport` cannot express: a receipt the
    server no longer holds, or a document that could not be read as a record.
    Those are differences too, and a command that stayed silent about them would
    report a clean verdict for a record that is simply gone.
    """

    receipt_id: str
    source: Source
    report: VerificationReport = field(default_factory=VerificationReport)
    findings: tuple[str, ...] = ()

    @property
    def failed(self) -> bool:
        """Whether a dimension contradicted the record, or a finding stands.

        ``unknown`` is deliberately not a failure. A dimension reports `unknown`
        when the verifier holds no evidence either way — a consumer with no
        signed usage report, say — and treating that as a contradiction would
        make an honest clean run look broken, which is how a verification tool
        gets ignored.
        """
        if self.findings:
            return True
        verdicts = self.verdicts()
        return any(verdicts[dimension] == "fail" for dimension in DIMENSIONS)

    def verdicts(self) -> dict[str, str]:
        """Return the five dimensions by name, for rendering and for ``--json``."""
        return {name: str(getattr(self.report, name)) for name in DIMENSIONS}

    def to_payload(self) -> dict[str, Any]:
        """Return the whole verification as a JSON-ready mapping."""
        return {
            "receipt_id": self.receipt_id,
            "source": self.source,
            "all_pass": self.report.all_pass,
            "verdicts": self.verdicts(),
            "receipts_checked": self.report.total_receipts,
            "evidence_levels": dict(self.report.evidence_levels),
            "recomputed_earnings": dict(self.report.recomputed_earnings),
            "recomputed_spend": dict(self.report.recomputed_spend),
            "anomalies": list(self.report.anomalies),
            "findings": list(self.findings),
            "failed": self.failed,
        }



def claimed_leaf_bytes(
    record: SignedRecord, stored: ReceiptRecord, findings: list[str]
) -> bytes | None:
    """Return the leaf bytes the receipt's own signed fields commit to.

    The leaf is rebuilt from the receipt's ``settled_at`` rather than read from
    the ``log_leaves`` row, because ``settled_at`` sits inside the signed payload
    and the stored ``committed_at`` does not. Folding *that* leaf against the
    stored tree is what makes an edited ``committed_at`` a detectable
    contradiction instead of a self-consistent rewrite.

    Args:
        record: The receipt's signed record.
        stored: The receipt row, for the claimed index and hash.
        findings: Appended when the payload carries no usable ``settled_at``.

    Returns:
        The canonical leaf encoding, or `None`.
    """
    settled_at = record.payload.get("settled_at")
    if not isinstance(settled_at, str):
        findings.append(f"receipt {stored.receipt_id} carries no settled_at in its payload")
        return None
    try:
        moment = dt.datetime.fromisoformat(settled_at)
    except ValueError as exc:
        findings.append(
            f"receipt {stored.receipt_id} carries an unusable settled_at {settled_at!r}: {exc}"
        )
        return None
    if moment.tzinfo is None:
        findings.append(f"receipt {stored.receipt_id} carries a naive settled_at")
        return None
    return LogEntry(
        index=stored.log_index,
        receipt_hash=stored.receipt_hash,
        committed_at=moment.astimezone(dt.UTC),
    ).leaf_bytes()



# --------------------------------------------------------------------------
# Receipt verification, consumer side: the three documents HTTP can supply
# --------------------------------------------------------------------------


def verify_receipt_from_server(
    *,
    receipt_document: Mapping[str, Any],
    checkpoint_document: Mapping[str, Any],
    history_document: Mapping[str, Any],
) -> ReceiptVerification:
    """Verify one receipt from the documents a consumer fetched over HTTP.

    The inputs are ``GET /v1/receipts/{id}``, ``GET /v1/checkpoint`` and
    ``GET /v1/account/history``, passed in as parsed documents so this function
    does no I/O and a test can drive it without a socket. Three things are
    recomputed rather than believed:

    * the leaf bytes come from the receipt's own signed ``settled_at``, so the
      served proof has to fold a leaf the receipt itself commits to;
    * the checkpoint's root is accepted only after its signature verifies under
      the published settlement key, so an unsigned or re-signed checkpoint
      degrades `ledger_included` to ``unknown`` instead of passing;
    * the allocation contract is re-hashed and compared with the digest bound
      into the receipt, so a contract served alongside a receipt cannot vouch
      for a split the receipt never promised.

    Args:
        receipt_document: The receipt response, carrying the signed record, its
            inclusion proof and the allocation contract it settled under.
        checkpoint_document: The checkpoint response, carrying the trusted root,
            its signature and the public key that authenticates it.
        history_document: The caller's own account history response.

    Returns:
        The verification. `metering_provenance` is ``unknown`` here by
        construction: a consumer holds no signed usage report, and inventing one
        from the receipt's declared level would be exactly the false comfort the
        five dimensions exist to avoid.

    Raises:
        ProtocolError: If a document is missing a field or carries one of the
            wrong shape. A malformed answer is refused rather than verified as
            far as it goes, because a partial document produces a partial
            verdict that reads as a clean one.
    """
    findings: list[str] = []
    receipt_id = _require_str(receipt_document, "receipt_id", "the receipt response")
    record = signed_record_from_dict(
        _require_mapping(receipt_document, "receipt", "the receipt response")
    )
    served_hash = _require_str(receipt_document, "receipt_hash", "the receipt response")
    if record.hash != served_hash:
        findings.append(
            f"the server served receipt {receipt_id} under hash {served_hash} but its own "
            f"payload hashes to {record.hash}; the body and its index disagree"
        )

    registry = _registry_from_checkpoint(checkpoint_document, findings)
    checkpoint, checkpoint_key = _checkpoint_from(checkpoint_document, findings)
    proof = _inclusion_proof(
        _require_mapping(receipt_document, "inclusion_proof", "the receipt response")
    )
    log_index = _require_int(receipt_document, "log_index", "the receipt response")
    leaf_bytes = claimed_leaf_bytes(
        record,
        ReceiptRecord(
            receipt_id=receipt_id,
            request_id=str(record.payload.get("request_id", "")),
            receipt_hash=served_hash,
            log_index=log_index,
            receipt_json="",
            created_at=dt.datetime.now(dt.UTC),
        ),
        findings,
    )
    contract = _contract_from(receipt_document.get("allocation_contract"))
    contracts: dict[str, AllocationContract] = {}
    contract_hash = str(record.payload.get("allocation_contract_hash", ""))
    if contract is not None:
        digest = canonical_digest(contract.digest_payload())
        if digest != contract_hash:
            findings.append(
                f"the server served allocation contract {contract.contract_id} "
                f"v{contract.version} hashing to {digest}, but receipt {receipt_id} is "
                f"bound to {contract_hash or '<none>'}; the served split is not the "
                "promised one"
            )
        else:
            contracts[contract_hash] = contract

    promised = _promised_receipt_ids(history_document)
    if receipt_id not in promised:
        findings.append(
            f"receipt {receipt_id} does not appear in account "
            f"{history_document.get('account_id', '<unknown>')!s}'s history, so the "
            "server's own books do not list the charge it served a receipt for"
        )

    report = verify_offline(
        VerificationInput(
            registry=registry,
            receipts=(record,),
            contracts=contracts,
            inclusion_proofs={} if proof is None else {record.hash: proof},
            leaf_bytes={} if leaf_bytes is None else {record.hash: leaf_bytes},
            trusted_checkpoint=checkpoint,
            checkpoint_public_key=checkpoint_key,
            # Only the receipt under verification is promised: the consumer holds
            # no local cache of the others, and naming them all would report every
            # past charge as "missing locally" on every run.
            expected_receipt_ids=(receipt_id,) if receipt_id in promised else (),
            expected_sequence=_expected_sequence(history_document),
            observed_sequence=_observed_sequence(history_document),
        )
    )
    return ReceiptVerification(
        receipt_id=receipt_id, source="server-http", report=report, findings=tuple(findings)
    )


def _registry_from_checkpoint(
    checkpoint_document: Mapping[str, Any], findings: list[str]
) -> IdentityRegistry:
    """Build a registry holding the settlement key a checkpoint publishes.

    The key arrives from the same server that serves the receipt, so this
    authenticates the receipt against the platform's *claimed* key. That is the
    honest limit of the HTTP path and the reason a consumer who wants a root the
    server cannot rewrite pins ``public_key_b64`` out of band.

    Args:
        checkpoint_document: The checkpoint response.
        findings: Appended when no usable key is published.

    Returns:
        A registry, empty when the key is missing or malformed — which makes
        `signature_valid` fail rather than pass.
    """
    registry = IdentityRegistry()
    public_key_b64 = checkpoint_document.get("public_key_b64")
    key_id = checkpoint_document.get("key_id")
    platform_id = checkpoint_document.get("platform_id")
    if (
        not isinstance(public_key_b64, str)
        or not public_key_b64
        or not isinstance(key_id, str)
        or not isinstance(platform_id, str)
    ):
        findings.append(
            "the checkpoint published no usable settlement public key, so the receipt's "
            "signature cannot be checked; upgrade the server or pin the key out of band"
        )
        return registry
    try:
        # `PROVISIONED_AT` is a fixed epoch, not a claim about when the platform
        # generated the key: `IdentityKey.is_valid_at` refuses a record signed
        # before its key was registered, and a key learned from a response has no
        # registration history of its own. Only the signature vouches for the key.
        registry.register_key(
            key_id, platform_id, "platform_settlement", public_key_b64, PROVISIONED_AT
        )
    except PotluckError as exc:
        findings.append(f"the checkpoint's settlement public key is unusable: {exc}")
    return registry


def _checkpoint_from(
    checkpoint_document: Mapping[str, Any], findings: list[str]
) -> tuple[Checkpoint | None, Any | None]:
    """Rebuild the signed checkpoint and the public key that authenticates it.

    Args:
        checkpoint_document: The checkpoint response.
        findings: Appended when either half is missing or malformed.

    Returns:
        ``(checkpoint, public_key)``, either of which may be `None`. `verify`
        degrades `ledger_included` to ``unknown`` without both, which is the
        correct answer: an unanchored root proves only that the server agrees
        with itself.
    """
    try:
        checkpoint = Checkpoint(
            platform_id=_require_str(checkpoint_document, "platform_id", "the checkpoint"),
            tree_size=_require_int(checkpoint_document, "tree_size", "the checkpoint"),
            root_hash=_require_str(checkpoint_document, "root_hash", "the checkpoint"),
            checkpoint_epoch=_require_int(
                checkpoint_document, "checkpoint_epoch", "the checkpoint"
            ),
            issued_at=dt.datetime.fromisoformat(
                _require_str(checkpoint_document, "issued_at", "the checkpoint")
            ),
            key_id=_require_str(checkpoint_document, "key_id", "the checkpoint"),
            signature=_require_str(checkpoint_document, "signature", "the checkpoint"),
        )
    except (PotluckError, ValueError) as exc:
        findings.append(f"the checkpoint response could not be read: {exc}")
        return None, None
    public_key_b64 = checkpoint_document.get("public_key_b64")
    if not isinstance(public_key_b64, str) or not public_key_b64:
        return checkpoint, None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        raw = base64_decode(public_key_b64)
        if len(raw) != 32:
            raise VerificationError(f"an Ed25519 public key is 32 bytes, got {len(raw)}")
        return checkpoint, Ed25519PublicKey.from_public_bytes(raw)
    except (PotluckError, ValueError) as exc:
        findings.append(f"the checkpoint's public key could not be loaded: {exc}")
        return checkpoint, None


def _inclusion_proof(body: Mapping[str, Any]) -> InclusionProof | None:
    """Rebuild an inclusion proof from its wire payload.

    Returns:
        The proof, or `None` when the payload is malformed. `None` makes
        `ledger_included` fail with "no inclusion proof", which is the right
        verdict for a proof that cannot be parsed.
    """
    try:
        siblings = cast("Sequence[Any]", body["siblings"])
        directions = cast("Sequence[Any]", body["directions"])
        return InclusionProof(
            index=int(cast("Any", body["index"])),
            tree_size=int(cast("Any", body["tree_size"])),
            leaf_hash=str(body["leaf_hash"]),
            root_hash=str(body["root_hash"]),
            siblings=tuple(str(part) for part in siblings),
            directions=tuple(str(part) for part in directions),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _contract_from(body: Any) -> AllocationContract | None:
    """Rebuild an allocation contract from the digest payload a server serves.

    Returns:
        The contract, or `None` when the response carried none or a malformed
        one. `None` makes `allocation_valid` fail with "contract not held
        locally", which is honest: without the weights the split cannot be
        recomputed at all.
    """
    if not isinstance(body, Mapping):
        return None
    try:
        weights = cast("Mapping[str, Any]", body["weights"])
        platform_account = body.get("platform_account")
        return AllocationContract(
            contract_id=str(body["contract_id"]),
            version=int(cast("Any", body["version"])),
            weights={str(name): int(value) for name, value in weights.items()},
            platform_fee_weight=int(cast("Any", body["platform_fee_weight"])),
            platform_account=None if platform_account is None else str(platform_account),
            rounding_rule=str(body["rounding_rule"]),
        )
    except (KeyError, TypeError, ValueError, PotluckError):
        return None


def _promised_receipt_ids(history_document: Mapping[str, Any]) -> frozenset[str]:
    """Return the receipt ids a server says touched this account."""
    listed = history_document.get("receipt_ids")
    if not isinstance(listed, Sequence) or isinstance(listed, (str, bytes)):
        return frozenset()
    return frozenset(str(item) for item in listed if isinstance(item, str))


def _expected_sequence(history_document: Mapping[str, Any]) -> tuple[int, ...]:
    """Return the contiguous history positions a server promises.

    Derived from ``entry_count``, which the endpoint reports separately from the
    entry list, so a history that arrived truncated or with a position missing is
    contradicted by the count the same answer carried.

    Raises:
        ProtocolError: If the response carries no usable count.
    """
    count = _require_int(history_document, "entry_count", "the account history response")
    return tuple(range(1, count + 1))


def _observed_sequence(history_document: Mapping[str, Any]) -> tuple[int, ...]:
    """Return the history positions that actually arrived.

    Raises:
        ProtocolError: If the response carries no entry list.
    """
    entries = history_document.get("entries")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise ProtocolError("the account history response carries no entry list")
    positions: list[int] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ProtocolError("an account history entry is not a JSON object")
        positions.append(_require_int(entry, "seq", "an account history entry"))
    return tuple(positions)


def _require_str(body: Mapping[str, Any], key: str, context: str) -> str:
    """Return a required non-empty string field.

    Raises:
        ProtocolError: If the field is absent, empty, or not a string.
    """
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{context} carries no usable {key!r}")
    return value


def _require_int(body: Mapping[str, Any], key: str, context: str) -> int:
    """Return a required integer field. `bool` is refused: it is an `int` subclass.

    Raises:
        ProtocolError: If the field is absent or not an integer.
    """
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{context} carries no integer {key!r}")
    return value


def _require_mapping(body: Mapping[str, Any], key: str, context: str) -> dict[str, Any]:
    """Return a required object field.

    Raises:
        ProtocolError: If the field is absent or not a JSON object.
    """
    value = body.get(key)
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{context} carries no object {key!r}")
    return cast("dict[str, Any]", dict(value))


# --------------------------------------------------------------------------
# Node-side reconciliation: what this node recorded vs what the server claims
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeAttemptView:
    """The server's claim about one attempt, as its node endpoint returns it.

    Parsed once here so `reconcile_node` works from typed fields and a test can
    build a view directly instead of staging an HTTP response.
    """

    attempt_id: str
    request_id: str
    dispatch_epoch: int
    status: str
    usage_status: str
    usage_tokens: int | None
    settlement_status: str
    execution_state: str | None
    receipt_id: str | None
    receipt_hash: str | None
    log_index: int | None

    @classmethod
    def from_payload(cls, body: Mapping[str, Any]) -> NodeAttemptView:
        """Build a view from one entry of the node attempts response.

        Raises:
            ProtocolError: If a required field is absent or of the wrong shape.
        """
        receipt = body.get("receipt")
        if receipt is not None and not isinstance(receipt, Mapping):
            raise ProtocolError(
                f"attempt {body.get('attempt_id')!r} carries a receipt that is not an object"
            )
        tokens = body.get("usage_tokens")
        if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, int)):
            raise ProtocolError(
                f"attempt {body.get('attempt_id')!r} carries a non-integer usage_tokens"
            )
        settled = receipt if isinstance(receipt, Mapping) else None
        return cls(
            attempt_id=_require_str(body, "attempt_id", "a node attempt"),
            request_id=_require_str(body, "request_id", "a node attempt"),
            dispatch_epoch=_require_int(body, "dispatch_epoch", "a node attempt"),
            status=_require_str(body, "status", "a node attempt"),
            usage_status=_require_str(body, "usage_status", "a node attempt"),
            settlement_status=_require_str(body, "settlement_status", "a node attempt"),
            execution_state=_optional_str(body.get("execution_state")),
            usage_tokens=None if tokens is None else int(tokens),
            receipt_id=None if settled is None else _optional_str(settled.get("receipt_id")),
            receipt_hash=None if settled is None else _optional_str(settled.get("receipt_hash")),
            log_index=(
                None
                if settled is None or not isinstance(settled.get("log_index"), int)
                else int(cast("int", settled["log_index"]))
            ),
        )

    @property
    def settled(self) -> bool:
        """Whether the server says this attempt was billed."""
        return self.settlement_status == "SETTLED"

    @property
    def terminal(self) -> bool:
        """Whether the server will do no more work on this attempt."""
        return self.status in _TERMINAL_ATTEMPT_STATUSES

    def describe(self) -> str:
        """Render the server's claim, restricted to the fields compared here."""
        receipt = self.receipt_id or "<none>"
        tokens = "unknown" if self.usage_tokens is None else str(self.usage_tokens)
        return (
            f"status={self.status} usage_tokens={tokens} "
            f"settlement={self.settlement_status} receipt={receipt}"
        )


def _optional_str(value: Any) -> str | None:
    """Return `value` as a string, or `None` when it is absent or empty."""
    return value if isinstance(value, str) and value else None


def reconcile_node(
    *,
    node_id: str,
    executions: Sequence[ExecutionRecord],
    ack_cursors: Mapping[str, int],
    spool: Sequence[SpoolRecord],
    remote_attempts: Sequence[NodeAttemptView],
) -> list[Difference]:
    """Diff this node's own durable record against the server's claim.

    Both directions are checked. A local execution the server never heard of is
    work the contributor may have paid an upstream for and never been billed for;
    a server attempt with no local execution is a dispatch this node has no
    record of running, which is either a lost database or a server inventing
    work. Field-level disagreements — epoch, request, terminal state, upstream
    call count — are reported separately, because each points at a different
    side.

    The local record is what the connector wrote: ``executions`` for what this
    node started and how it ended, ``ack_cursors`` for how far the server
    acknowledged, and ``spool`` for signed events still undelivered. A
    ``usage.final`` left in the spool is a report this node signed with its
    device key, so it is compared against the server's settlement state rather
    than merely counted.

    Args:
        node_id: Named in a finding when the response belongs to another node.
        executions: Every local execution row.
        ack_cursors: Acknowledged sequence per attempt.
        spool: Every still-unacknowledged spooled event.
        remote_attempts: The server's view of this node's attempts.

    Returns:
        The differences, ordered by attempt so the same input renders the same
        report. Empty means the two sides agree on everything compared here.
    """
    del node_id  # the endpoint scopes the answer; the name is context for callers
    differences: list[Difference] = []
    remote = {attempt.attempt_id: attempt for attempt in remote_attempts}
    local = {execution.attempt_id: execution for execution in executions}
    spooled_finals = _spooled_final_reports(spool)

    for attempt_id in sorted(set(local) | set(remote)):
        execution = local.get(attempt_id)
        claim = remote.get(attempt_id)
        if execution is None:
            assert claim is not None
            differences.append(
                Difference(
                    subject=f"attempt {attempt_id}",
                    kind="missing_locally",
                    local="<no execution row>",
                    remote=claim.describe(),
                )
            )
            continue
        if claim is None:
            differences.append(
                Difference(
                    subject=f"attempt {attempt_id}",
                    kind="missing_at_server",
                    local=_describe_execution(execution, ack_cursors),
                    remote="<the server lists no such attempt for this node>",
                )
            )
            continue
        differences.extend(_compare_attempt(execution, claim, ack_cursors))
        differences.extend(_compare_spool(attempt_id, execution, claim, spooled_finals))
    return differences


def _describe_execution(execution: ExecutionRecord, ack_cursors: Mapping[str, int]) -> str:
    """Render a local execution row, restricted to the fields compared here."""
    return (
        f"request={execution.request_id} epoch={execution.epoch} state={execution.state} "
        f"upstream_calls={execution.upstream_calls} "
        f"acked_seq={ack_cursors.get(execution.attempt_id, 0)}"
    )


def _spooled_final_reports(spool: Sequence[SpoolRecord]) -> dict[str, list[SpoolRecord]]:
    """Group the still-unacknowledged ``usage.final`` events by attempt.

    A spool row of this type is a usage report this node signed and the server
    has not acknowledged, which is the single most consequential thing a
    contributor can hold locally: it is the difference between being paid and
    not being paid for work already done.
    """
    grouped: dict[str, list[SpoolRecord]] = {}
    for record in spool:
        if record.event_type == "usage.final":
            grouped.setdefault(record.attempt_id, []).append(record)
    return grouped


def _compare_attempt(
    execution: ExecutionRecord, claim: NodeAttemptView, ack_cursors: Mapping[str, int]
) -> list[Difference]:
    """Compare one local execution with the server's claim about the same attempt."""
    differences: list[Difference] = []
    subject = f"attempt {execution.attempt_id}"
    local = _describe_execution(execution, ack_cursors)
    remote = claim.describe()

    def add(kind: str, *, local_text: str = local, remote_text: str = remote) -> None:
        differences.append(
            Difference(subject=subject, kind=kind, local=local_text, remote=remote_text)
        )

    if execution.request_id != claim.request_id:
        add("request_mismatch")
    if execution.epoch != claim.dispatch_epoch:
        add("epoch_mismatch")
    if execution.upstream_calls > 1:
        # `begin_execution` exists so a reconnect cannot issue a second upstream
        # call; more than one means either that guarantee broke or the row was
        # edited, and either way the contributor paid an upstream twice.
        add("repeated_upstream_call")
    if execution.upstream_calls < 1 and (claim.settled or claim.usage_tokens is not None):
        add("usage_without_upstream_call")
    if execution.state == "SUBMITTED" and not (claim.status == "COMPLETED" and claim.settled):
        add("unsettled_after_submit")
    if execution.state == "FAILED" and (claim.status == "COMPLETED" or claim.settled):
        add("settled_after_local_failure")
    if execution.state == "SUBMITTING" and claim.terminal:
        add("execution_never_finished")
    if execution.state == "UNKNOWN" and not claim.terminal:
        # The node cannot say whether the upstream consumed tokens, and neither
        # can the server: the attempt is open on both sides with no outcome. A
        # settled claim resolves it, so only an unresolved one is reported.
        add("outcome_unresolved")
    if claim.settled and claim.receipt_id is None:
        add("settled_without_receipt")
    return differences


def _compare_spool(
    attempt_id: str,
    execution: ExecutionRecord,
    claim: NodeAttemptView,
    spooled_finals: Mapping[str, Sequence[SpoolRecord]],
) -> list[Difference]:
    """Compare this node's undelivered signed reports with the server's state."""
    pending = spooled_finals.get(attempt_id)
    if not pending:
        return []
    subject = f"attempt {attempt_id}"
    local = (
        f"state={execution.state} signed usage.final at seq "
        f"{sorted(record.attempt_seq for record in pending)}"
    )
    if claim.settled:
        return [
            Difference(
                subject=subject,
                kind="ack_cursor_behind",
                local=local,
                remote=f"settled as {claim.receipt_id or '<unknown receipt>'}",
            )
        ]
    if claim.terminal:
        return [
            Difference(
                subject=subject,
                kind="report_never_delivered",
                local=local,
                remote=claim.describe(),
            )
        ]
    # Still open on both sides: the report is queued and the attempt has not
    # reached a terminal state, which is an in-flight node, not a difference.
    return []


