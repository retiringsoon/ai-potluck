"""The contribution node's durable store.

The server store's own suite lives in the ``ai-potluck-server`` repository,
beside ``ServerStore``; what is covered here is the node half of the shared
plumbing: the execution guard that makes a reconnect unable to issue a second
upstream call, the spool and its acknowledgement cursor, node metadata, and
the open-existing refusal that keeps a lost ``node.db`` from being rebuilt as
an empty database.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import pytest

from aipotluck.errors import StateTransitionError
from aipotluck.persistence import ExecutionRecord, NodeStore

T0 = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
T1 = T0 + dt.timedelta(seconds=1)
T2 = T0 + dt.timedelta(seconds=2)


@pytest.fixture
def node(tmp_path: Path) -> Iterator[NodeStore]:
    """A node store on a fresh file, closed at the end of the test."""
    store = NodeStore.open(tmp_path / "node.db")
    yield store
    store.close()


def test_node_restart_preserves_ack_cursor_and_executions(tmp_path: Path) -> None:
    path = tmp_path / "node.db"
    store = NodeStore.open(path)
    record, created = store.begin_execution(
        attempt_id="A1", request_id="R1", epoch=0, request_digest="sha256:d1", at=T0
    )
    assert created is True
    store.enqueue_spool(
        attempt_id="A1", attempt_seq=1, event_type="output.delta", payload={"text": "a"}, at=T0
    )
    store.enqueue_spool(
        attempt_id="A1", attempt_seq=2, event_type="output.delta", payload={"text": "b"}, at=T0
    )
    store.ack_through("A1", 1, at=T1)
    store.meta_set("node_id", "N1")
    store.close()

    reopened = NodeStore.open(path)
    try:
        assert reopened.get_execution("A1") == record
        assert reopened.ack_cursor("A1") == 1
        assert [s.attempt_seq for s in reopened.unacked_spool("A1")] == [2]
        assert reopened.meta_get("node_id") == "N1"
    finally:
        reopened.close()


# -- 5. node execution guard and interrupted marking ------------------------

def test_begin_execution_is_new_only_the_first_time(node: NodeStore) -> None:
    first, created_first = node.begin_execution(
        attempt_id="A1", request_id="R1", epoch=0, request_digest="sha256:d1", at=T0
    )
    second, created_second = node.begin_execution(
        attempt_id="A1", request_id="R1", epoch=0, request_digest="sha256:d1", at=T1
    )

    assert created_first is True
    assert created_second is False
    assert second == first
    assert second.state == "SUBMITTING"
    assert second.upstream_calls == 0


def test_record_upstream_call_returns_new_count(node: NodeStore) -> None:
    node.begin_execution(
        attempt_id="A1", request_id="R1", epoch=0, request_digest="sha256:d1", at=T0
    )

    assert node.record_upstream_call("A1") == 1
    assert node.record_upstream_call("A1") == 2
    assert node.get_execution("A1").upstream_calls == 2


def test_record_upstream_call_unknown_execution_raises(node: NodeStore) -> None:
    with pytest.raises(StateTransitionError):
        node.record_upstream_call("ghost")


def test_mark_interrupted_as_unknown_flips_only_submitting(node: NodeStore) -> None:
    node.begin_execution(
        attempt_id="A1", request_id="R1", epoch=0, request_digest="d", at=T0
    )
    node.begin_execution(
        attempt_id="A2", request_id="R2", epoch=0, request_digest="d", at=T0
    )
    node.finish_execution("A2", "SUBMITTED", at=T1)
    node.begin_execution(
        attempt_id="A3", request_id="R3", epoch=0, request_digest="d", at=T0
    )
    node.finish_execution("A3", "FAILED", at=T1)

    changed = node.mark_interrupted_as_unknown(at=T2)

    assert changed == ["A1"]
    assert node.get_execution("A1").state == "UNKNOWN"
    assert node.get_execution("A2").state == "SUBMITTED"
    assert node.get_execution("A3").state == "FAILED"
    # Running it again finds nothing left to interrupt.
    assert node.mark_interrupted_as_unknown(at=T2) == []


def test_open_executions_lists_only_unfinished(node: NodeStore) -> None:
    node.begin_execution(attempt_id="A1", request_id="R1", epoch=0, request_digest="d", at=T0)
    node.begin_execution(attempt_id="A2", request_id="R2", epoch=0, request_digest="d", at=T0)
    node.finish_execution("A2", "FAILED", at=T1)

    assert [e.attempt_id for e in node.open_executions()] == ["A1"]
    assert node.open_executions()[0] == ExecutionRecord(
        attempt_id="A1",
        request_id="R1",
        epoch=0,
        request_digest="d",
        state="SUBMITTING",
        upstream_calls=0,
        started_at=T0,
        finished_at=None,
    )


# -- 6. spool acknowledgement -----------------------------------------------


def test_ack_through_removes_prefix_and_keeps_rest(node: NodeStore) -> None:
    node.begin_execution(attempt_id="A1", request_id="R1", epoch=0, request_digest="d", at=T0)
    for seq in range(1, 5):
        node.enqueue_spool(
            attempt_id="A1", attempt_seq=seq, event_type="output.delta",
            payload={"n": seq}, at=T0,
        )

    cursor = node.ack_through("A1", 2, at=T1)

    assert cursor == 2
    assert [s.attempt_seq for s in node.unacked_spool("A1")] == [3, 4]
    assert node.ack_cursor("A1") == 2
    # An ack below the cursor does not move it backwards.
    assert node.ack_through("A1", 1, at=T2) == 2
    assert [s.attempt_seq for s in node.unacked_spool("A1")] == [3, 4]


def test_enqueue_spool_is_idempotent_per_sequence(node: NodeStore) -> None:
    first = node.enqueue_spool(
        attempt_id="A1", attempt_seq=1, event_type="output.delta", payload={"n": 1}, at=T0
    )
    second = node.enqueue_spool(
        attempt_id="A1", attempt_seq=1, event_type="output.delta", payload={"n": 1}, at=T1
    )

    assert first == second
    assert len(node.unacked_spool("A1")) == 1


def test_ack_cursor_defaults_to_zero(node: NodeStore) -> None:
    assert node.ack_cursor("never") == 0
    assert node.unacked_spool("never") == []


# -- node meta and lifecycle ------------------------------------------------


def test_node_meta_roundtrip_and_default(node: NodeStore) -> None:
    assert node.meta_get("missing") is None
    node.meta_set("node_id", "N1")
    node.meta_set("node_id", "N2")
    assert node.meta_get("node_id") == "N2"


def test_open_existing_opens_the_database_open_created(tmp_path: Path) -> None:
    """The positive control: the check refuses a missing file, not every file.

    Without this a check that always raised would satisfy both refusals above and
    break every audit command in production instead.
    """
    path = tmp_path / "node.db"
    created = NodeStore.open(path)
    created.meta_set("node_id", "N1")
    created.close()

    reopened = NodeStore.open_existing(path)
    try:
        assert reopened.meta_get("node_id") == "N1"
    finally:
        reopened.close()
