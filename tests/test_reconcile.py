"""Two-sided reconciliation, consumer and contributor side.

The node-side difference kinds are covered in isolation here, so a check that
stops firing is caught by name instead of only by an end-to-end test going
quietly green; the ``share reconcile`` refusal of a missing ``node.db`` is
covered through ``aipotluck.cli.main`` the way the installed console script
drives it, and needs no running center. The end-to-end reconciliations against
a real center (operator and consumer views included) live in the
``ai-potluck-server`` repository's suite, beside the center they drive against.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from pathlib import Path

import pytest

from aipotluck.canonical import canonicalize
from aipotluck.cli import main
from aipotluck.persistence import ExecutionRecord, SpoolRecord
from aipotluck.reconcile import Difference, NodeAttemptView, reconcile_node

MODEL = "coding-pool"


def _kinds(differences: Sequence[Difference]) -> list[str]:
    """The difference kinds, for asserting which check fired."""
    return [item.kind for item in differences]


def _subjects(differences: Sequence[Difference]) -> list[str]:
    """The records the differences name."""
    return [item.subject for item in differences]



def _write_node_config_without_a_database(path: Path, database: Path) -> None:
    """Write a valid ``node.json`` whose ``database_path`` names nothing.

    Every field `load_node` requires is present, so the command gets as far as
    the database and fails on it instead of on the configuration. The adapter is
    a ``fake`` one, which carries no upstream credential, and the link token is a
    placeholder that is never sent anywhere: the refusal happens before any dial.
    """
    document = {
        "center_url": "ws://127.0.0.1:1/v1/node-link",
        "node_id": "node-without-a-database",
        "device_id": "node-without-a-database",
        "device_key_id": "device-key-1",
        "provider_id": "provider-1",
        "database_path": str(database),
        "token": "placeholder-link-token-never-sent",
        "device_private_key_b64": "placeholder-device-key-never-used",
        "adapter": {
            "kind": "fake",
            "endpoint": None,
            "api_key": None,
            "model": MODEL,
            "provider_style": "openai",
            "name": "fake-adapter",
        },
        "share_enabled": True,
        "max_concurrency": 1,
        "max_local_reserved_milli_credits": 20_000,
    }
    path.write_text(canonicalize(document).decode("utf-8"), encoding="utf-8")


def test_share_reconcile_refuses_a_node_database_that_does_not_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A contributor's missing ``node.db`` is reported, not silently replaced.

    Creating it would do two harms at once: every attempt the center lists would
    be reported as ``missing_locally``, which reads as "the center claims work I
    never did" rather than "I am looking at the wrong file", and the empty
    database would still be there for the next `share run` to continue from, as
    if this node had never executed anything. The refusal comes before the center
    is dialed, so this needs no running platform.
    """
    database = tmp_path / "never-started" / "node.db"
    config = tmp_path / "node.json"
    _write_node_config_without_a_database(config, database)

    assert main(["share", "reconcile", "--config", str(config)]) == 1
    error = capsys.readouterr().err
    assert str(database) in error
    assert "does not exist" in error
    assert "potluck share run" in error, "the message must say what to do next"
    assert not database.exists(), "the command must not create the database it refused"
    assert not database.parent.exists(), "nor the directory that would have held it"


# The node-side difference kinds, in isolation
# ---------------------------------------------------------------------------


def _execution(
    attempt_id: str = "att-1",
    *,
    request_id: str = "req-1",
    epoch: int = 1,
    state: str = "SUBMITTED",
    upstream_calls: int = 1,
) -> ExecutionRecord:
    """Build a local execution row."""
    moment = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)
    return ExecutionRecord(
        attempt_id=attempt_id,
        request_id=request_id,
        epoch=epoch,
        request_digest="sha256:digest",
        state=state,
        upstream_calls=upstream_calls,
        started_at=moment,
        finished_at=moment,
    )


def _claim(
    attempt_id: str = "att-1",
    *,
    request_id: str = "req-1",
    epoch: int = 1,
    status: str = "COMPLETED",
    usage_tokens: int | None = 1000,
    settlement_status: str = "SETTLED",
    receipt_id: str | None = "R-1",
) -> NodeAttemptView:
    """Build the center's claim about one attempt."""
    return NodeAttemptView(
        attempt_id=attempt_id,
        request_id=request_id,
        dispatch_epoch=epoch,
        status=status,
        usage_status="COMPLETE" if usage_tokens is not None else "UNKNOWN",
        usage_tokens=usage_tokens,
        settlement_status=settlement_status,
        execution_state="SUBMITTED",
        receipt_id=receipt_id,
        receipt_hash="sha256:receipt",
        log_index=0 if receipt_id is not None else None,
    )


def _diff(
    *,
    executions: Sequence[ExecutionRecord] = (),
    remote: Sequence[NodeAttemptView] = (),
    spool: Sequence[SpoolRecord] = (),
) -> list[Difference]:
    """Run the node-side comparison over hand-built rows."""
    return reconcile_node(
        node_id="node-1",
        executions=executions,
        ack_cursors={},
        spool=spool,
        remote_attempts=remote,
    )


def test_matching_records_produce_no_difference() -> None:
    """The happy path is silent, so a difference always means something."""
    assert _diff(executions=[_execution()], remote=[_claim()]) == []


def test_an_attempt_the_center_never_heard_of_is_named() -> None:
    """Work this node ran and the center does not list is the contributor's loss."""
    differences = _diff(executions=[_execution("att-9")])
    assert _kinds(differences) == ["missing_at_center"]
    assert _subjects(differences) == ["attempt att-9"]


def test_an_attempt_with_no_local_execution_is_named() -> None:
    """A dispatch this node has no record of running is named from the other side."""
    differences = _diff(remote=[_claim("att-9")])
    assert _kinds(differences) == ["missing_locally"]
    assert _subjects(differences) == ["attempt att-9"]


def test_an_epoch_disagreement_is_named() -> None:
    """A redispatch the two sides numbered differently is a named difference."""
    differences = _diff(executions=[_execution(epoch=1)], remote=[_claim(epoch=2)])
    assert _kinds(differences) == ["epoch_mismatch"]


def test_a_request_disagreement_is_named() -> None:
    """A local row bound to the wrong request is named, not merged into another."""
    differences = _diff(
        executions=[_execution(request_id="req-local")],
        remote=[_claim(request_id="req-remote")],
    )
    assert "request_mismatch" in _kinds(differences)


def test_a_submitted_attempt_the_center_never_settled_is_named() -> None:
    """The node reported final usage and the center billed nothing."""
    differences = _diff(
        executions=[_execution()],
        remote=[_claim(status="FAILED", usage_tokens=None, settlement_status="PENDING",
                       receipt_id=None)],
    )
    assert "unsettled_after_submit" in _kinds(differences)


def test_a_settled_attempt_with_no_receipt_is_named() -> None:
    """The center says it billed but names no receipt: there is nothing to verify."""
    differences = _diff(executions=[_execution()], remote=[_claim(receipt_id=None)])
    assert "settled_without_receipt" in _kinds(differences)


def test_an_unresolved_unknown_outcome_is_named() -> None:
    """A node that cannot say, and a center that has not decided, is an open loss.

    ``UNKNOWN`` means the process died around the upstream call, so tokens may
    have been consumed. If the center reached a terminal state the outcome is
    settled one way or the other and there is nothing to report; if it has not,
    the contributor is carrying an unbounded exposure.
    """
    unresolved = _diff(
        executions=[_execution(state="UNKNOWN")],
        remote=[_claim(status="OFFERED", usage_tokens=None, settlement_status="PENDING",
                       receipt_id=None)],
    )
    assert "outcome_unresolved" in _kinds(unresolved)

    resolved = _diff(executions=[_execution(state="UNKNOWN")], remote=[_claim()])
    assert "outcome_unresolved" not in _kinds(resolved)


def test_an_execution_that_never_finished_is_named() -> None:
    """A row left in ``SUBMITTING`` under a terminal center attempt never closed out."""
    differences = _diff(
        executions=[_execution(state="SUBMITTING")],
        remote=[_claim(status="FAILED", usage_tokens=None, settlement_status="PENDING",
                       receipt_id=None)],
    )
    assert "execution_never_finished" in _kinds(differences)


def _spooled_final(attempt_id: str = "att-1", seq: int = 3) -> SpoolRecord:
    """Build a still-unacknowledged signed usage report."""
    return SpoolRecord(
        spool_id=1,
        attempt_id=attempt_id,
        attempt_seq=seq,
        event_type="usage.final",
        payload={"event_type": "usage.final", "seq": seq},
        enqueued_at=dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC),
    )


def test_a_signed_report_the_center_never_acknowledged_is_named() -> None:
    """A ``usage.final`` still in the spool is a report this node signed and lost.

    This is the most consequential thing a contributor can hold locally: the
    difference between being paid and not being paid for work already done.
    """
    never_delivered = _diff(
        executions=[_execution(state="FAILED")],
        remote=[_claim(status="FAILED", usage_tokens=None, settlement_status="PENDING",
                       receipt_id=None)],
        spool=[_spooled_final()],
    )
    assert "report_never_delivered" in _kinds(never_delivered)

    behind = _diff(executions=[_execution()], remote=[_claim()], spool=[_spooled_final()])
    assert "ack_cursor_behind" in _kinds(behind)


def test_a_spooled_report_on_a_still_open_attempt_is_not_a_difference() -> None:
    """An in-flight node has a queued report and an open attempt; that is normal.

    Reporting it would make every reconcile run during a live request noisy, and
    a noisy reconciliation is one nobody runs.
    """
    differences = _diff(
        executions=[_execution(state="SUBMITTING")],
        remote=[_claim(status="EXECUTING", usage_tokens=None, settlement_status="PENDING",
                       receipt_id=None)],
        spool=[_spooled_final()],
    )
    assert "report_never_delivered" not in _kinds(differences)
    assert "ack_cursor_behind" not in _kinds(differences)


def test_a_node_attempt_view_is_parsed_from_the_wire_shape() -> None:
    """The endpoint's JSON becomes a typed view, with an absent receipt staying absent."""
    view = NodeAttemptView.from_payload({
        "attempt_id": "att-1",
        "request_id": "req-1",
        "dispatch_epoch": 2,
        "status": "COMPLETED",
        "usage_status": "COMPLETE",
        "usage_tokens": 1000,
        "settlement_status": "SETTLED",
        "execution_state": "SUBMITTED",
        "updated_at": "2026-10-06T12:00:00+00:00",
        "receipt": {"receipt_id": "R-1", "receipt_hash": "sha256:x", "log_index": 4},
    })
    assert view.settled and view.terminal
    assert (view.receipt_id, view.log_index) == ("R-1", 4)

    unsettled = NodeAttemptView.from_payload({
        "attempt_id": "att-2",
        "request_id": "req-2",
        "dispatch_epoch": 1,
        "status": "OFFERED",
        "usage_status": "UNKNOWN",
        "usage_tokens": None,
        "settlement_status": "PENDING",
        "execution_state": None,
        "updated_at": "2026-10-06T12:00:00+00:00",
        "receipt": None,
    })
    assert not unsettled.settled and not unsettled.terminal
    assert unsettled.receipt_id is None and unsettled.usage_tokens is None


def test_a_node_attempt_view_refuses_a_malformed_receipt() -> None:
    """A receipt that is not an object is refused rather than read as absent."""
    from aipotluck.errors import ProtocolError

    with pytest.raises(ProtocolError):
        NodeAttemptView.from_payload({
            "attempt_id": "att-1",
            "request_id": "req-1",
            "dispatch_epoch": 1,
            "status": "COMPLETED",
            "usage_status": "COMPLETE",
            "usage_tokens": 1,
            "settlement_status": "SETTLED",
            "execution_state": None,
            "updated_at": "2026-10-06T12:00:00+00:00",
            "receipt": "R-1",
        })
