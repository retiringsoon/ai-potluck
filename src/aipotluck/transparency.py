"""Append-only Merkle transparency log with signed checkpoints.

Why this lives in the client package: verifying inclusion and checkpoint
signatures is something a client does without trusting the server, so the log
primitives are part of the protocol layer. Server bookkeeping and the gateway
live in ``ai-potluck-server``, which imports these primitives to keep its own
books.

Modeled on Certificate Transparency structures (RFC 9162): each committed
journal contributes a leaf, the tree root is signed periodically, and clients
verify inclusion for their own receipts plus consistency against roots they
cached earlier.

What this does and does not prove matters. An inclusion proof shows a receipt is
in the log. It does not show the recipient list is complete, that all postings
sum to zero, or that funds are realizable. A consistency proof shows the tree a
client already saw is an unchanged leftmost prefix of the tree the log serves now,
at any tree size, which catches a log that rewrote or dropped history. It does
not exclude a log that serves different views to different clients: nothing here
attests that two clients were shown the same tree, so a split view survives
without an independent witness, which this package does not provide.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .canonical import base64_decode, base64_encode, canonicalize, sign_payload, verify_payload
from .errors import VerificationError

_EMPTY_LEAF_HASH = bytes(32)


def _leaf_hash(entry: bytes) -> bytes:
    """Hash a log entry with a 0x00 domain prefix so it cannot collide with a node."""
    import hashlib

    return hashlib.sha256(b"\x00" + entry).digest()


def _node_hash(left: bytes, right: bytes) -> bytes:
    """Hash an interior node with a 0x01 prefix."""
    import hashlib

    return hashlib.sha256(b"\x01" + left + right).digest()


def _hex(data: bytes) -> str:
    return data.hex()


def _split(leaves: Sequence[bytes]) -> tuple[list[bytes], list[bytes]]:
    """Split leaves into the largest power-of-two left subtree and the rest.

    This is the RFC 9162 decomposition, so the left subtree is always perfectly
    balanced. Root computation, inclusion proofs, and consistency proofs all use
    this same split; if they disagreed, no proof would ever verify.
    """
    count = len(leaves)
    if count < 2:
        return list(leaves), []
    split = 1
    while split * 2 < count:
        split *= 2
    return list(leaves[:split]), list(leaves[split:])


def _left_size(count: int) -> int:
    """Return the left subtree size for `count` leaves under the shared split.

    This is the largest power of two strictly below `count`, which is *not*
    `count // 2` once `count` stops being a power of two (for 3 leaves the split
    is 2+1, not 1+2). Every descent must use this, or a proof will not fold back
    to the root.
    """
    if count < 2:
        return count
    split = 1
    while split * 2 < count:
        split *= 2
    return split


def _root_bytes(leaves: Sequence[bytes]) -> bytes:
    """Return the Merkle root over `leaves`."""
    if not leaves:
        return _EMPTY_LEAF_HASH
    if len(leaves) == 1:
        return leaves[0]
    left, right = _split(leaves)
    if not right:
        return _root_bytes(left)
    return _node_hash(_root_bytes(left), _root_bytes(right))


def _root_of(leaves: Sequence[bytes]) -> str:
    """Return the hex Merkle root over `leaves`."""
    return _hex(_root_bytes(leaves))


def _proof_siblings(leaves: Sequence[bytes], index: int) -> tuple[list[str], list[str]]:
    """Return the audit path for `index`.

    `directions[i]` records where the sibling sits relative to the running hash:
    `"left"` means the sibling is the left input of the node hash, so
    verification computes `node(sibling, current)`. A leaf inside the left
    subtree therefore records `"right"` for its right-subtree sibling.

    The walk descends from the root while verification folds upward from the
    leaf, so the path is emitted in reverse visit order.
    """
    top_down: list[str] = []
    top_down_dirs: list[str] = []
    current = list(leaves)
    position = index
    while len(current) > 1:
        left, right = _split(current)
        if not right:
            # A perfect subtree: keep descending without emitting a sibling.
            current = left
            continue
        if position < len(left):
            top_down.append(_root_of(right))
            top_down_dirs.append("right")
            current = left
        else:
            top_down.append(_root_of(left))
            top_down_dirs.append("left")
            position -= len(left)
            current = right
    return top_down[::-1], top_down_dirs[::-1]


def verify_inclusion(proof: InclusionProof, leaf_bytes: bytes) -> bool:
    """Verify an inclusion proof against the leaf's canonical bytes.

    Args:
        proof: The inclusion proof to check.
        leaf_bytes: The leaf's canonical encoding.

    Returns:
        True when folding the audit path reproduces the stated root.
    """
    current = _leaf_hash(leaf_bytes)
    for sibling_hex, direction in zip(proof.siblings, proof.directions, strict=True):
        sibling = bytes.fromhex(sibling_hex)
        if direction == "left":
            current = _node_hash(sibling, current)
        else:
            current = _node_hash(current, sibling)
    return _hex(current) == proof.root_hash


def _consistency_plan(old_size: int, new_size: int) -> list[tuple[int, int]]:
    """Return the leaf ranges a consistency proof must carry, in proof order.

    This is the RFC 9162 section 2.1.2 `PROOF(m, D[n])` / `SUBPROOF(m, D[n], b)`
    recursion, run over the shared `_split` decomposition, so it holds for every
    `1 <= old_size < new_size` rather than only power-of-two old sizes.

    Each entry is an `(offset, size)` leaf range whose root the proof supplies.
    The offsets are what make a non-power-of-two old size provable: the verifier
    learns which subtree every node is supposed to be instead of trusting a bare
    fold order, and can therefore tell a 3-leaf root from a 4-leaf root even
    though neither contains the other.

    Args:
        old_size: The previously observed tree size, at least 1.
        new_size: The current tree size, strictly greater than `old_size`.

    Returns:
        Ranges in bottom-up order, the order a verifier consumes them.
    """
    plan: list[tuple[int, int]] = []

    def subproof(count: int, offset: int, size: int, is_whole: bool) -> None:
        # `count` of this subtree's `size` leading leaves are already in the
        # old tree; the rest were appended afterwards.
        if count == size:
            # This subtree is exactly the old tree, so its root is the client's
            # cached old_root rather than something the proof has to carry.
            # Reaching that with `is_whole` clear means we descended into a
            # right-hand sibling, whose root the caller does need.
            if not is_whole:
                plan.append((offset, size))
            return
        left = _left_size(size)
        if count <= left:
            # The old tree ends inside the left subtree, so every leaf of the
            # right subtree is new and its root is a fresh proof node.
            subproof(count, offset, left, is_whole)
            plan.append((offset + left, size - left))
        else:
            # The old tree swallows the whole left subtree, so that root must
            # be proven, and the recursion continues into the right one.
            subproof(count - left, offset + left, size - left, False)
            plan.append((offset, left))

    subproof(old_size, 0, new_size, True)
    return plan


def _consistency_nodes(
    leaves: Sequence[bytes], old_size: int, new_size: int
) -> list[str]:
    """Return the proof node hex values for a consistency proof.

    Args:
        leaves: The leaves of the new tree.
        old_size: The previously observed tree size, at least 1.
        new_size: The current tree size, strictly greater than `old_size`.

    Returns:
        One hex node per range in `_consistency_plan`, in the same order.
    """
    return [
        _root_of(leaves[offset : offset + size])
        for offset, size in _consistency_plan(old_size, new_size)
    ]


def _consistency_proof_length(old_size: int, new_size: int) -> int:
    """Return how many nodes a consistency proof carries for these tree sizes.

    Derived from the same recursion the generator uses, so the length check in
    `verify_consistency` cannot drift from what `MerkleLog.consistency_proof`
    actually emits.
    """
    if old_size <= 0 or old_size >= new_size:
        return 0
    return len(_consistency_plan(old_size, new_size))


def _root_from_ranges(offset: int, size: int, known: dict[tuple[int, int], bytes]) -> bytes | None:
    """Rebuild the root of the leaf range at `offset` from `known`, if it can.

    Returns None when `known` does not pin that range down. Callers decide
    whether that is a failure or simply means the range was never restated.
    """
    if (root := known.get((offset, size))) is not None:
        return root
    if size == 1:
        return None
    left = _left_size(size)
    left_root = _root_from_ranges(offset, left, known)
    if left_root is None:
        return None
    right_root = _root_from_ranges(offset + left, size - left, known)
    if right_root is None:
        return None
    return _node_hash(left_root, right_root)


def verify_consistency(
    old_size: int,
    old_root: str,
    new_size: int,
    new_root: str,
    proof: Sequence[str],
) -> bool:
    """Verify that a tree of `old_size` leaves is a prefix of one of `new_size`.

    Works for every `1 <= old_size <= new_size`, not only power-of-two old sizes.
    The proof nodes are interpreted through `_consistency_plan`, the same
    recursion the generator uses, so each node is checked against the exact leaf
    range it is supposed to summarize.

    Two things are checked, and both matter. Where the plan restates the old
    range, the old root must equal the root rebuilt from those nodes, which is
    what rules out a rewritten prefix at a non-power-of-two size. The old root is
    then used as the root of that range to rebuild the new root. For a
    power-of-two old size the first check has nothing to restate, because the old
    tree is a genuine subtree of the new one and the client's cached root is the
    only claim about it.

    Args:
        old_size: Tree size the client previously observed.
        old_root: Hex root at that size.
        new_size: Current tree size.
        new_root: Hex root at the current size.
        proof: Proof nodes from `MerkleLog.consistency_proof`.

    Returns:
        True when the proof shows the old tree is an unchanged leftmost prefix
        of the new one.

    Raises:
        VerificationError: If the proof length does not match the two sizes, or
            a node is not valid hex.
    """
    if old_size > new_size:
        return False
    if old_size == 0:
        return old_root == _hex(_EMPTY_LEAF_HASH)
    if old_size == new_size:
        return old_root == new_root and not proof
    try:
        old_bytes = bytes.fromhex(old_root)
    except ValueError as exc:
        raise VerificationError(f"old root is not valid hex: {exc}") from exc
    plan = _consistency_plan(old_size, new_size)
    if len(proof) != len(plan):
        raise VerificationError(
            f"consistency proof for ({old_size} -> {new_size}) must carry "
            f"{len(plan)} node(s), got {len(proof)}"
        )
    known: dict[tuple[int, int], bytes] = {}
    for (offset, size), node_hex in zip(plan, proof, strict=True):
        try:
            known[(offset, size)] = bytes.fromhex(node_hex)
        except ValueError as exc:
            raise VerificationError(
                f"consistency proof node for ({old_size} -> {new_size}) covering "
                f"leaves [{offset}, {offset + size}) is not valid hex: {exc}"
            ) from exc
    # Where the plan restates the old range, hold it to the old root.
    restated = _root_from_ranges(0, old_size, known)
    if restated is not None and restated != old_bytes:
        return False
    # The plan plus the old root must rebuild the new root exactly.
    combined = dict(known)
    combined[(0, old_size)] = old_bytes
    rebuilt = _root_from_ranges(0, new_size, combined)
    if rebuilt is None:
        raise VerificationError(
            f"consistency proof for ({old_size} -> {new_size}) does not supply "
            "enough nodes to rebuild the new root"
        )
    return _hex(rebuilt) == new_root


@dataclass(frozen=True)
class LogEntry:
    """One committed receipt commitment in the log."""

    index: int
    receipt_hash: str
    committed_at: dt.datetime

    def leaf_bytes(self) -> bytes:
        """Return the canonical leaf encoding.

        The leaf carries only the index, the receipt digest, and the commit
        time. Prompts, credentials, and raw bodies never enter the public log.
        """
        return canonicalize(
            {
                "index": self.index,
                "receipt_hash": self.receipt_hash,
                "committed_at": self.committed_at.astimezone(dt.UTC).isoformat(),
            }
        )


@dataclass(frozen=True)
class InclusionProof:
    """Proof that a leaf sits at a given index under a given root."""

    index: int
    tree_size: int
    leaf_hash: str
    root_hash: str
    siblings: tuple[str, ...]
    directions: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical payload for transmission."""
        return {
            "index": self.index,
            "tree_size": self.tree_size,
            "leaf_hash": self.leaf_hash,
            "root_hash": self.root_hash,
            "siblings": list(self.siblings),
            "directions": list(self.directions),
        }


@dataclass
class MerkleLog:
    """Append-only Merkle tree over receipt commitments."""

    def __init__(self) -> None:
        self._leaves: list[bytes] = []
        self._entries: list[LogEntry] = []

    def append(self, receipt_hash: str, committed_at: dt.datetime) -> int:
        """Append a receipt commitment and return its leaf index.

        Args:
            receipt_hash: The canonical digest of a settlement receipt.
            committed_at: Commit time, part of the leaf so the same digest at
                different times stays distinguishable.

        Returns:
            The zero-based leaf index.
        """
        index = len(self._leaves)
        entry = LogEntry(
            index=index,
            receipt_hash=receipt_hash,
            committed_at=committed_at.astimezone(dt.UTC),
        )
        self._entries.append(entry)
        self._leaves.append(_leaf_hash(entry.leaf_bytes()))
        return index

    def size(self) -> int:
        """Return the number of leaves."""
        return len(self._leaves)

    def entries(self) -> list[LogEntry]:
        """Return every entry in append order."""
        return list(self._entries)

    def root(self) -> str:
        """Return the current Merkle root as hex.

        An empty log hashes to 32 zero bytes, matching the fixed-size empty root
        convention.
        """
        return _hex(_root_bytes(self._leaves))

    def inclusion_proof(self, index: int) -> InclusionProof:
        """Build an inclusion proof for a leaf.

        Args:
            index: The leaf index.

        Returns:
            The proof.

        Raises:
            VerificationError: If the index is outside the log.
        """
        if index < 0 or index >= len(self._leaves):
            raise VerificationError(
                f"leaf index {index} is outside the log of size {len(self._leaves)}"
            )
        siblings, directions = _proof_siblings(self._leaves, index)
        return InclusionProof(
            index=index,
            tree_size=len(self._leaves),
            leaf_hash=_hex(self._leaves[index]),
            root_hash=self.root(),
            siblings=tuple(siblings),
            directions=tuple(directions),
        )

    def consistency_proof(self, old_size: int, new_size: int | None = None) -> dict[str, Any]:
        """Build a proof that the tree of `old_size` leaves is a prefix of the new one.

        Every `1 <= old_size <= size()` is supported, not just power-of-two sizes.
        Under the shared power-of-two split a 3-leaf root is not a subtree of a
        4-leaf root, so this cannot work by appending and folding upward. Instead
        the proof names the leaf range each node summarizes, and carries enough of
        the old tree's own interior to show the first `old_size` leaves are
        unchanged. At a power-of-two `old_size` the old tree is a real subtree of
        the new one, so the proof carries only the appended ranges and the client's
        cached root is what pins the prefix.

        Args:
            old_size: The previously observed tree size.
            new_size: The current tree size; defaults to the current size.

        Returns:
            A payload with the old root, new root, and proof nodes.

        Raises:
            VerificationError: If `old_size` exceeds the current size, since a log
                cannot shrink.
        """
        target = new_size if new_size is not None else len(self._leaves)
        if old_size > target:
            raise VerificationError(
                f"old tree size {old_size} is larger than current size {target}; "
                "a log cannot shrink"
            )
        if old_size <= 0:
            return {
                "old_size": 0,
                "new_size": target,
                "old_root": _hex(_EMPTY_LEAF_HASH),
                "new_root": _root_of(self._leaves[:target]),
                "proof": [],
            }
        if old_size == target:
            return {
                "old_size": old_size,
                "new_size": target,
                "old_root": _root_of(self._leaves[:old_size]),
                "new_root": _root_of(self._leaves[:target]),
                "proof": [],
            }
        return {
            "old_size": old_size,
            "new_size": target,
            "old_root": _root_of(self._leaves[:old_size]),
            "new_root": _root_of(self._leaves[:target]),
            "proof": _consistency_nodes(self._leaves[:target], old_size, target),
        }


@dataclass(frozen=True)
class Checkpoint:
    """A periodically signed statement of the log's current root."""

    platform_id: str
    tree_size: int
    root_hash: str
    checkpoint_epoch: int
    issued_at: dt.datetime
    key_id: str
    signature: str = ""

    def payload(self) -> dict[str, Any]:
        """Return the unsigned canonical payload."""
        return {
            "platform_id": self.platform_id,
            "tree_size": self.tree_size,
            "root_hash": self.root_hash,
            "checkpoint_epoch": self.checkpoint_epoch,
            "issued_at": self.issued_at.astimezone(dt.UTC).isoformat(),
            "key_id": self.key_id,
        }

    def sign(self, private_key: Any) -> Checkpoint:
        """Sign the checkpoint with the platform key and return a new instance."""
        signature = sign_payload(
            "AIPotluck/transparency-checkpoint/v1", self.payload(), private_key
        )
        return Checkpoint(
            platform_id=self.platform_id,
            tree_size=self.tree_size,
            root_hash=self.root_hash,
            checkpoint_epoch=self.checkpoint_epoch,
            issued_at=self.issued_at,
            key_id=self.key_id,
            signature=signature,
        )

    def verify(self, public_key: Any) -> bool:
        """Verify the checkpoint signature.

        Raises:
            VerificationError: If the signature is missing.
        """
        if not self.signature:
            raise VerificationError(
                f"checkpoint for epoch {self.checkpoint_epoch} carries no signature"
            )
        return verify_payload(
            "AIPotluck/transparency-checkpoint/v1", self.payload(), self.signature, public_key
        )


@dataclass
class CheckpointHistory:
    """A party's cached checkpoints, used to detect history rewrites."""

    platform_id: str
    checkpoints: list[Checkpoint] = field(default_factory=list)

    def observe(self, checkpoint: Checkpoint) -> None:
        """Cache a checkpoint, rejecting a non-monotonic epoch or tree size.

        Raises:
            VerificationError: If the epoch does not advance or the tree shrinks.
        """
        if self.checkpoints:
            last = self.checkpoints[-1]
            if checkpoint.checkpoint_epoch <= last.checkpoint_epoch:
                raise VerificationError(
                    f"checkpoint epoch {checkpoint.checkpoint_epoch} does not advance "
                    f"past {last.checkpoint_epoch}"
                )
            if checkpoint.tree_size < last.tree_size:
                raise VerificationError(
                    f"tree size went backwards: {last.tree_size} -> {checkpoint.tree_size}"
                )
        self.checkpoints.append(checkpoint)

    def latest(self) -> Checkpoint | None:
        """Return the newest cached checkpoint."""
        return self.checkpoints[-1] if self.checkpoints else None


def verify_checkpoint_history(
    history: CheckpointHistory,
    consistency_proofs: dict[int, dict[str, Any]],
) -> list[str]:
    """Cross-check cached checkpoints against supplied consistency proofs.

    Args:
        history: The client's cached checkpoints, oldest first.
        consistency_proofs: Map of old tree size to proof payload from the log.

    Returns:
        Anomaly descriptions. When nothing was detectable the list still carries
        an explicit note that a split view is not excluded, because absence of
        evidence here is not evidence of absence.
    """
    anomalies: list[str] = []
    for previous, current in zip(history.checkpoints, history.checkpoints[1:], strict=False):
        if previous.tree_size == current.tree_size:
            if previous.root_hash != current.root_hash:
                anomalies.append(
                    f"same tree size {current.tree_size} carries two different roots"
                )
            continue
        proof = consistency_proofs.get(previous.tree_size)
        if proof is None:
            anomalies.append(f"no consistency proof supplied for old size {previous.tree_size}")
            continue
        if proof.get("old_root") != previous.root_hash:
            anomalies.append(
                f"consistency proof for size {previous.tree_size} starts from "
                f"{proof.get('old_root')}, cached root is {previous.root_hash}"
            )
            continue
        if proof.get("new_root") != current.root_hash:
            anomalies.append(
                f"consistency proof ends at {proof.get('new_root')}, "
                f"cached root is {current.root_hash}"
            )
            continue
        try:
            holds = verify_consistency(
                int(proof["old_size"]),
                str(proof["old_root"]),
                int(proof["new_size"]),
                str(proof["new_root"]),
                list(proof.get("proof", [])),
            )
        except VerificationError as exc:
            anomalies.append(f"consistency proof unusable: {exc}")
            continue
        if not holds:
            anomalies.append(
                f"consistency proof from size {previous.tree_size} does not reach "
                f"the cached root at size {current.tree_size}"
            )
    if not anomalies:
        anomalies.append(
            "no split-view detected among supplied checkpoints; a malicious log "
            "serving different roots to different clients is not excluded without "
            "an independent witness"
        )
    return anomalies


def encode_proof(proof: InclusionProof) -> str:
    """Serialize an inclusion proof to base64 canonical JSON."""
    return base64_encode(canonicalize(proof.to_payload()))


def decode_proof(encoded: str) -> InclusionProof:
    """Parse a base64-encoded inclusion proof.

    Raises:
        VerificationError: If the payload is malformed.
    """
    import json

    try:
        payload = json.loads(base64_decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise VerificationError(f"inclusion proof is not valid JSON: {exc}") from exc
    try:
        return InclusionProof(
            index=int(payload["index"]),
            tree_size=int(payload["tree_size"]),
            leaf_hash=str(payload["leaf_hash"]),
            root_hash=str(payload["root_hash"]),
            siblings=tuple(str(s) for s in payload["siblings"]),
            directions=tuple(str(d) for d in payload["directions"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise VerificationError(f"inclusion proof is missing fields: {exc}") from exc