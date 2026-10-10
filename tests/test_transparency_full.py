"""Consistency proofs across every tree size, not just powers of two.

The power-of-two split makes this genuinely hard: a 3-leaf root is
`node(node(l0, l1), l2)` while a 4-leaf root is `node(node(l0, l1), node(l2, l3))`,
so the old root is not a subtree of the new one and cannot simply be folded
upward. These tests pin the real behaviour down over all 190 (old, new) pairs
for sizes 1..20, and check that a proof which does not describe the actual
prefix is rejected rather than waved through.
"""

from __future__ import annotations

import datetime as dt
import itertools

import pytest

from aipotluck.errors import VerificationError
from aipotluck.transparency import (
    MerkleLog,
    _consistency_plan,
    _consistency_proof_length,
    verify_consistency,
    verify_inclusion,
)

MAX_SIZE = 20
_COMMIT_TIME = dt.datetime(2026, 10, 6, tzinfo=dt.UTC)

# Every (old_size, new_size) pair with 1 <= old_size <= new_size <= MAX_SIZE.
_PAIRS = list(itertools.product(range(1, MAX_SIZE + 1), repeat=2))
_PAIRS = [(old, new) for old, new in _PAIRS if old <= new]
_GROWING_PAIRS = [(old, new) for old, new in _PAIRS if old < new]


def _log_of_size(size: int) -> MerkleLog:
    """Return a log holding `size` distinct receipts."""
    log = MerkleLog()
    for index in range(size):
        log.append(f"sha256:{index:064d}", _COMMIT_TIME)
    return log


def _flip_hex(value: str) -> str:
    """Return `value` with its first hex digit changed."""
    return ("0" if value[0] != "0" else "1") + value[1:]


@pytest.mark.parametrize(("old_size", "new_size"), _GROWING_PAIRS)
def test_every_size_pair_verifies(old_size: int, new_size: int) -> None:
    """An honest proof verifies for every old/new size pair, odd sizes included."""
    log = _log_of_size(new_size)
    payload = log.consistency_proof(old_size)

    assert payload["old_size"] == old_size
    assert payload["new_size"] == new_size
    assert verify_consistency(
        old_size,
        payload["old_root"],
        new_size,
        payload["new_root"],
        payload["proof"],
    ), f"honest proof rejected for ({old_size} -> {new_size})"


@pytest.mark.parametrize(("old_size", "new_size"), _GROWING_PAIRS)
def test_proof_length_matches_the_length_check(old_size: int, new_size: int) -> None:
    """The generator's node count is exactly what `verify_consistency` expects.

    These two are derived from the same recursion, so this guards against the
    verifier demanding a different shape than the generator produces.
    """
    log = _log_of_size(new_size)
    proof = log.consistency_proof(old_size)["proof"]

    assert len(proof) == _consistency_proof_length(old_size, new_size)
    assert len(proof) == len(_consistency_plan(old_size, new_size))


@pytest.mark.parametrize(("old_size", "new_size"), _GROWING_PAIRS)
def test_honest_proof_rejects_a_different_new_root(old_size: int, new_size: int) -> None:
    """An honest proof does not verify against someone else's new root."""
    log = _log_of_size(new_size)
    payload = log.consistency_proof(old_size)
    other = _log_of_size(new_size)
    other.append("sha256:" + "f" * 64, _COMMIT_TIME)

    assert other.root() != payload["new_root"]
    assert not verify_consistency(
        old_size,
        payload["old_root"],
        new_size,
        other.root(),
        payload["proof"],
    ), f"proof for ({old_size} -> {new_size}) accepted a foreign new root"


@pytest.mark.parametrize(("old_size", "new_size"), _GROWING_PAIRS)
def test_doctored_proof_node_does_not_verify(old_size: int, new_size: int) -> None:
    """Flipping one hex digit in any proof node breaks verification."""
    log = _log_of_size(new_size)
    payload = log.consistency_proof(old_size)
    assert payload["proof"], "expected a non-empty proof for a growing tree"

    for index in range(len(payload["proof"])):
        doctored = list(payload["proof"])
        doctored[index] = _flip_hex(doctored[index])
        assert not verify_consistency(
            old_size,
            payload["old_root"],
            new_size,
            payload["new_root"],
            doctored,
        ), f"doctored node {index} verified for ({old_size} -> {new_size})"


@pytest.mark.parametrize(("old_size", "new_size"), _GROWING_PAIRS)
def test_rewritten_prefix_is_rejected(old_size: int, new_size: int) -> None:
    """A log that altered an old leaf cannot prove consistency against the old root.

    This is the case a power-of-two-only implementation could not express at all:
    the client holds a root for a tree the log no longer agrees with, and the
    proof has to be able to say so.
    """
    log = _log_of_size(new_size)
    payload = log.consistency_proof(old_size)

    # Same size, different history: one early leaf swapped out.
    rewritten = MerkleLog()
    for index in range(new_size):
        digest = "b" * 64 if index == 0 else f"{index:064d}"
        rewritten.append(f"sha256:{digest}", _COMMIT_TIME)

    assert rewritten.root() != payload["new_root"]
    assert not verify_consistency(
        old_size,
        payload["old_root"],
        new_size,
        rewritten.root(),
        payload["proof"],
    ), f"rewritten history verified for ({old_size} -> {new_size})"


@pytest.mark.parametrize(("old_size", "new_size"), _GROWING_PAIRS)
def test_wrong_old_root_does_not_verify(old_size: int, new_size: int) -> None:
    """An otherwise honest proof is rejected when the cached old root is wrong.

    This is the check that binds the client's cached root to the prefix. When
    `old_size` is not a power of two the old tree is not a subtree of the new one,
    so the new root can often be rebuilt from the proof nodes without ever reading
    `old_root`. Without a separate check that the proof agrees with the old root,
    a proof would verify against any old root at all, which is precisely the
    rewrite this whole exercise exists to detect.
    """
    log = _log_of_size(new_size)
    payload = log.consistency_proof(old_size)

    assert not verify_consistency(
        old_size,
        _flip_hex(payload["old_root"]),
        new_size,
        payload["new_root"],
        payload["proof"],
    ), f"proof for ({old_size} -> {new_size}) accepted a wrong old root"


@pytest.mark.parametrize("old_size", range(1, MAX_SIZE + 1))
def test_equal_sizes_need_no_proof_nodes(old_size: int) -> None:
    """A tree is trivially consistent with itself, and says so with an empty proof."""
    log = _log_of_size(old_size)
    payload = log.consistency_proof(old_size)

    assert payload["proof"] == []
    assert verify_consistency(
        old_size, payload["old_root"], old_size, payload["new_root"], []
    )
    assert not verify_consistency(
        old_size, payload["old_root"], old_size, _flip_hex(payload["new_root"]), []
    )


def test_wrong_length_proof_raises_with_sizes_named() -> None:
    """A proof of the wrong length is an error naming both sizes, not a silent pass."""
    log = _log_of_size(6)
    payload = log.consistency_proof(3)

    with pytest.raises(VerificationError, match=r"\(3 -> 6\)"):
        verify_consistency(
            3, payload["old_root"], 6, payload["new_root"], payload["proof"][:-1]
        )


def test_old_size_larger_than_new_size_is_false() -> None:
    """A log cannot shrink, so an old size beyond the new one is rejected."""
    log = _log_of_size(4)
    payload = log.consistency_proof(2)

    assert not verify_consistency(
        4, payload["new_root"], 2, payload["old_root"], payload["proof"]
    )


def test_consistency_proof_refuses_to_shrink() -> None:
    """Asking for a proof from a larger old size than the log has is an error."""
    log = _log_of_size(4)

    with pytest.raises(VerificationError, match="cannot shrink"):
        log.consistency_proof(5)


@pytest.mark.parametrize("size", range(1, MAX_SIZE + 1))
def test_inclusion_still_verifies_for_every_leaf(size: int) -> None:
    """The consistency rewrite left inclusion proofs working at every size."""
    log = _log_of_size(size)
    leaves = {entry.index: entry.leaf_bytes() for entry in log.entries()}

    for index in range(size):
        assert verify_inclusion(log.inclusion_proof(index), leaves[index]), index


def test_consistency_holds_across_a_sequence_of_checkpoints() -> None:
    """Stepwise verification from every earlier size to the final one all holds."""
    log = MerkleLog()
    roots: list[str] = []
    for index in range(MAX_SIZE):
        log.append(f"sha256:{index:064d}", _COMMIT_TIME)
        roots.append(log.root())

    for old_size, old_root in enumerate(roots, start=1):
        payload = log.consistency_proof(old_size)
        assert payload["old_root"] == old_root
        assert verify_consistency(
            old_size, old_root, log.size(), log.root(), payload["proof"]
        ), old_size
