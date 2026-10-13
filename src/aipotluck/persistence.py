"""Durable persistence for the contribution node.

The node keeps a durable record of what it already executed, so a reconnect can
never issue a second upstream call, and of what it already produced, so nothing
is lost when the link drops before the server acknowledges it.

The server's own store (``ServerStore``, its schema and its event plumbing)
lives in the ``ai-potluck-server`` package; this module holds the shared
``_Store`` plumbing carved down to the node's four tables, the ``NodeStore``
built on it, and the record shapes both sides speak (``ExecutionRecord``,
``SpoolRecord``, ``ReceiptRecord``).

Only the standard library is used. The project has exactly one runtime
dependency (``cryptography``) and adding another is not allowed, so this module
speaks SQLite directly.

Concurrency strategy
--------------------
Each store instance gives every thread its own `sqlite3.Connection` through
`threading.local()`, and every mutating operation runs inside
``BEGIN IMMEDIATE``. The alternative — one connection shared by all threads and
guarded by a single `RLock` — is simpler, but it would (a) require disabling
``check_same_thread`` and (b) serialize every read behind the writers' lock. The
per-thread design keeps the default ``check_same_thread=True`` (a connection is
created and used by exactly one thread) and pushes the critical section down to
SQLite's own write lock, so the insert-or-fetch in
:meth:`NodeStore.begin_execution` is correct at the database level rather than
only within one process. With WAL enabled, readers do not block behind the
single writer.

Pragmas and why each matters
----------------------------
* ``busy_timeout=5000`` — a second writer that reaches ``BEGIN IMMEDIATE`` waits
  for the first to commit instead of failing immediately with ``SQLITE_BUSY``.
* ``journal_mode=WAL`` — lets readers proceed while a writer holds the write
  lock, and keeps the database consistent across a crash. Restart durability is
  the entire point of the module, so this is not optional.
* ``foreign_keys=ON`` — SQLite disables foreign keys by default.

Timestamps
----------
Every instant is stored as ISO 8601 UTC text in a fixed format
(``%Y-%m-%dT%H:%M:%S.%f%z``), so lexical order equals chronological order and a
``WHERE created_at > ?`` predicate is a plain string comparison.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self, cast

from .canonical import canonicalize, loads_strict
from .errors import PotluckError, StateTransitionError

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"

def _to_text(value: dt.datetime) -> str:
    """Render an instant as fixed-width ISO 8601 UTC text.

    Raises:
        ValueError: If the value is naive, because a naive instant has no
            well-defined position on the timeline and would make ordering depend
            on the host's local zone.
    """
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(dt.UTC).strftime(_TS_FORMAT)


def _from_text(value: str) -> dt.datetime:
    """Parse fixed-width ISO 8601 UTC text written by `_to_text`."""
    return dt.datetime.strptime(value, _TS_FORMAT).astimezone(dt.UTC)



def _dump_payload(payload: dict[str, Any]) -> str:
    """Serialize a stored JSON payload in canonical form."""
    return canonicalize(payload).decode("utf-8")


def _load_payload(text: str) -> dict[str, Any]:
    """Parse a stored payload back into a mapping.

    Raises:
        PotluckError: If the stored bytes are not a JSON object, which means the
            row was corrupted outside this module.
    """
    value = loads_strict(text)
    if not isinstance(value, dict):
        raise PotluckError("stored payload is not a JSON object")
    return cast("dict[str, Any]", value)



# --------------------------------------------------------------------------
# Record shapes. `ReceiptRecord` is defined here, on the verification side of
# the split, because the consumer's receipt verification rebuilds one from the
# documents HTTP can supply; the server store in `ai-potluck-server` imports it
# rather than growing a second spelling of the same row.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceiptRecord:
    """An issued receipt, keyed by `receipt_id`."""

    receipt_id: str
    request_id: str
    receipt_hash: str
    log_index: int
    receipt_json: str
    created_at: dt.datetime



@dataclass(frozen=True)
class ExecutionRecord:
    """The node's durable record of one attempt execution."""

    attempt_id: str
    request_id: str
    epoch: int
    request_digest: str
    state: str
    upstream_calls: int
    started_at: dt.datetime
    finished_at: dt.datetime | None


@dataclass(frozen=True)
class SpoolRecord:
    """An event the node has produced but not yet had acknowledged."""

    spool_id: int
    attempt_id: str
    attempt_seq: int
    event_type: str
    payload: dict[str, Any]
    enqueued_at: dt.datetime



_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    attempt_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    request_digest TEXT NOT NULL,
    state TEXT NOT NULL,
    upstream_calls INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS spool (
    spool_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL,
    attempt_seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    UNIQUE (attempt_id, attempt_seq)
);

CREATE TABLE IF NOT EXISTS ack_cursors (
    attempt_id TEXT PRIMARY KEY,
    acked_seq INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

class _Store:
    """Shared plumbing: per-thread connections, pragmas, transactions, schema."""

    #: Milliseconds a writer waits for the SQLite write lock before giving up.
    #: Five seconds comfortably covers a competing commit that is merely slow,
    #: while still failing loudly rather than hanging forever.
    _BUSY_TIMEOUT_MS = 5000

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        parent = Path(self._path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._tls = threading.local()
        self._registry_lock = threading.Lock()
        # Keyed by owning thread id: sqlite3 refuses to close a connection from a
        # thread other than the one that created it, so the registry identifies
        # which connections `close` may close directly and which it can only drop.
        self._connections: dict[int, sqlite3.Connection] = {}
        self._closed = False
        with self._immediate(self._connection()) as conn:
            conn.executescript(_SCHEMA)


    @classmethod
    def open(cls, path: str | Path) -> Self:
        """Open (or create) the store at `path` and ensure the schema exists.

        Creating is what the process that *owns* a database needs — a server
        started by ``potluck-server serve``, a node started by ``potluck share run``.
        A command that only opens somebody else's database calls
        `open_existing` instead, so a path that names nothing reports that fact
        rather than manufacturing an empty database to look at.
        """
        return cls(path)

    @classmethod
    def open_existing(cls, path: str | Path) -> Self:
        """Open the store whose database file already exists at `path`.

        `open` creates the file — and the parent directory — it is pointed at,
        which is fatal for a command that reads one. A lost or mistyped
        ``database_path`` would come back as a freshly created empty database,
        and an empty database reports no differences: ``admin reconcile`` over a
        server whose database is entirely gone would print that both sides agree
        and exit 0, which is the one answer an audit must never give. The same
        applies to a node database rebuilt from nothing, which a later
        ``share run`` would then continue with as if the node had never worked.

        Refusing before any connection is made keeps a missing file missing: no
        file and no directory is created on the way to the error.

        A zero-byte file is refused for the same reason. SQLite reads it as a
        valid empty database, but this store writes its schema as it creates
        one, so an empty file is a creation that never finished rather than a
        database with nothing to report.

        Raises:
            PotluckError: If `path` is not an existing file with content in it.
        """
        candidate = Path(path)
        if not candidate.is_file():
            raise PotluckError(
                f"database {candidate} does not exist: this command opens a "
                "database, it does not create one — start the process that owns "
                "it (`potluck-server serve` for a server, `potluck share run` for a "
                f"node), or correct the database_path that names {candidate}"
            )
        if candidate.stat().st_size == 0:
            raise PotluckError(
                f"database {candidate} is empty (0 bytes): no schema was ever "
                "written to it, so it holds no records to open — restore it from "
                "a backup, or delete it and let the process that owns it "
                "(`potluck-server serve` for a server, `potluck share run` for a node) "
                "create a fresh one"
            )
        return cls(path)

    def close(self) -> None:
        """Release this store's connections.

        Idempotent. After this the store must not be used again. Only the
        calling thread's connection can be closed directly, because sqlite3 with
        ``check_same_thread=True`` refuses a cross-thread close; the connections
        belonging to other threads are released by dropping our reference, which
        lets them be finalized as those threads exit. Call it after the worker
        threads that used the store have joined.
        """
        with self._registry_lock:
            if self._closed:
                return
            self._closed = True
            mine = self._connections.pop(threading.get_ident(), None)
            self._connections.clear()
        if mine is not None:
            mine.close()
        self._tls = threading.local()

    def _connection(self) -> sqlite3.Connection:
        """Return this thread's connection, creating it on first use.

        Raises:
            PotluckError: If the store has already been closed.
        """
        if self._closed:
            raise PotluckError("the store is closed")
        conn: sqlite3.Connection | None = getattr(self._tls, "conn", None)
        if conn is not None:
            return conn
        # isolation_level=None disables sqlite3's implicit transaction handling
        # so this module controls every BEGIN/COMMIT explicitly.
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self._BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._tls.conn = conn
        with self._registry_lock:
            self._connections[threading.get_ident()] = conn
        return conn

    @contextmanager
    def _immediate(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """Run a block inside a ``BEGIN IMMEDIATE`` write transaction.

        The immediate lock is taken before the first statement, so two
        concurrent callers cannot both read the pre-state and then both write:
        the second waits (up to the busy timeout) and then observes the first
        caller's committed result.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        conn.commit()


class NodeStore(_Store):
    """The contribution node's durable store: executions, the spool, and meta.

    It records what the node already started so a reconnect cannot issue a
    second upstream call, and what it already produced so nothing is lost when
    the link drops before the server acknowledges it.
    """

    def begin_execution(
        self,
        *,
        attempt_id: str,
        request_id: str,
        epoch: int,
        request_digest: str,
        at: dt.datetime,
    ) -> tuple[ExecutionRecord, bool]:
        """Start an execution, or return the existing one.

        Returns ``(record, True)`` on first start and ``(existing, False)`` when
        the attempt was already begun — the caller must not issue a second
        upstream call in that case. The check and the insert are a single
        ``INSERT ... ON CONFLICT DO NOTHING`` followed by a fetch, inside one
        transaction, so two racing reconnects cannot both believe they are first.
        """
        conn = self._connection()
        with self._immediate(conn):
            cur = conn.execute(
                "INSERT INTO executions (attempt_id, request_id, epoch, request_digest, state,"
                " upstream_calls, started_at, finished_at)"
                " VALUES (?, ?, ?, ?, 'SUBMITTING', 0, ?, NULL)"
                " ON CONFLICT(attempt_id) DO NOTHING",
                (attempt_id, request_id, epoch, request_digest, _to_text(at)),
            )
            inserted = cur.rowcount == 1
            row = conn.execute(
                "SELECT * FROM executions WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            return _execution_from_row(row), inserted

    def get_execution(self, attempt_id: str) -> ExecutionRecord | None:
        """Return a stored execution, or `None`."""
        conn = self._connection()
        row = conn.execute(
            "SELECT * FROM executions WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return None if row is None else _execution_from_row(row)

    def finish_execution(self, attempt_id: str, state: str, *, at: dt.datetime) -> None:
        """Mark an execution finished with a terminal state.

        The verdict is written once. A row that already carries a terminal state
        is never overwritten: a CANCEL that marked the attempt FAILED while the
        worker was mid-stream must survive the worker's own completion, or the
        node would report SUBMITTED for an attempt the server cancelled. The
        race is real — the cancel runs on the socket loop and the completion on
        the worker — so the guarantee lives in the WHERE clause rather than in
        every caller remembering to check first.

        Raises:
            StateTransitionError: If no such execution exists, or it already
                finished, naming the state it finished with.
        """
        conn = self._connection()
        with self._immediate(conn):
            cur = conn.execute(
                "UPDATE executions SET state = ?, finished_at = ?"
                " WHERE attempt_id = ? AND finished_at IS NULL",
                (state, _to_text(at), attempt_id),
            )
            if cur.rowcount == 0:
                row = conn.execute(
                    "SELECT state FROM executions WHERE attempt_id = ?", (attempt_id,)
                ).fetchone()
                if row is None:
                    raise StateTransitionError(f"unknown execution {attempt_id}")
                raise StateTransitionError(
                    f"execution {attempt_id} already finished as {row['state']};"
                    f" refusing to overwrite it with {state}"
                )

    def record_upstream_call(self, attempt_id: str) -> int:
        """Increment and return an execution's upstream-call count.

        The increment and the read happen in one transaction, so the returned
        value is the caller's own count: a first call returns 1.

        Raises:
            StateTransitionError: If no such execution exists.
        """
        conn = self._connection()
        with self._immediate(conn):
            cur = conn.execute(
                "UPDATE executions SET upstream_calls = upstream_calls + 1 WHERE attempt_id = ?",
                (attempt_id,),
            )
            if cur.rowcount == 0:
                raise StateTransitionError(f"unknown execution {attempt_id}")
            row = conn.execute(
                "SELECT upstream_calls FROM executions WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            return int(row["upstream_calls"])

    def open_executions(self) -> list[ExecutionRecord]:
        """Return executions that have not finished, ordered by attempt ID."""
        conn = self._connection()
        rows = conn.execute(
            "SELECT * FROM executions WHERE finished_at IS NULL ORDER BY attempt_id"
        ).fetchall()
        return [_execution_from_row(row) for row in rows]

    def executions(self) -> list[ExecutionRecord]:
        """Return every execution this node started, ordered by attempt ID.

        Read-only whole-table view for reconciliation: unlike `open_executions`
        it includes finished rows, because a finished local execution the server
        knows nothing about is the difference a contributor needs to see.
        """
        conn = self._connection()
        rows = conn.execute("SELECT * FROM executions ORDER BY attempt_id").fetchall()
        return [_execution_from_row(row) for row in rows]

    def mark_interrupted_as_unknown(self, *, at: dt.datetime) -> list[str]:
        """Mark every still-``SUBMITTING`` execution as ``UNKNOWN``.

        Called once at node startup. A process that died mid-call may or may not
        have consumed upstream tokens, so the state becomes ``UNKNOWN`` and never
        zero; `at` is accepted for a consistent call shape across the recovery
        entry points. Executions already ``SUBMITTED`` or ``FAILED`` are left
        untouched because their outcome is known.

        Returns:
            The attempt IDs that were changed.
        """
        conn = self._connection()
        with self._immediate(conn):
            rows = conn.execute(
                "SELECT attempt_id FROM executions WHERE state = 'SUBMITTING' ORDER BY attempt_id"
            ).fetchall()
            attempt_ids = [str(row["attempt_id"]) for row in rows]
            if attempt_ids:
                conn.execute("UPDATE executions SET state = 'UNKNOWN' WHERE state = 'SUBMITTING'")
            return attempt_ids

    def enqueue_spool(
        self,
        *,
        attempt_id: str,
        attempt_seq: int,
        event_type: str,
        payload: dict[str, Any],
        at: dt.datetime,
    ) -> int:
        """Spool an event for delivery and return its spool ID.

        Idempotent on ``(attempt_id, attempt_seq)``: re-spooling a sequence that
        is already queued returns the existing spool ID rather than adding a
        duplicate to the send queue.
        """
        conn = self._connection()
        with self._immediate(conn):
            cur = conn.execute(
                "INSERT INTO spool (attempt_id, attempt_seq, event_type, payload_json,"
                " enqueued_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(attempt_id, attempt_seq) DO NOTHING",
                (attempt_id, attempt_seq, event_type, _dump_payload(payload), _to_text(at)),
            )
            if cur.rowcount == 1:
                spool_id = cur.lastrowid
                if spool_id is None:
                    raise PotluckError("sqlite did not assign a spool ID")
                return int(spool_id)
            row = conn.execute(
                "SELECT spool_id FROM spool WHERE attempt_id = ? AND attempt_seq = ?",
                (attempt_id, attempt_seq),
            ).fetchone()
            return int(row["spool_id"])

    def unacked_spool(self, attempt_id: str) -> list[SpoolRecord]:
        """Return the spooled events of an attempt that are not yet acknowledged."""
        conn = self._connection()
        rows = conn.execute(
            "SELECT * FROM spool WHERE attempt_id = ? ORDER BY attempt_seq", (attempt_id,)
        ).fetchall()
        return [_spool_from_row(row) for row in rows]

    def spooled(self) -> list[SpoolRecord]:
        """Return every still-unacknowledged spooled event, ordered by attempt then seq.

        `unacked_spool` answers for one attempt; reconciliation needs the whole
        queue at once, because an event this node signed and the server never
        acknowledged is a difference even when its attempt has long finished.
        """
        conn = self._connection()
        rows = conn.execute(
            "SELECT * FROM spool ORDER BY attempt_id, attempt_seq"
        ).fetchall()
        return [_spool_from_row(row) for row in rows]

    def ack_through(self, attempt_id: str, attempt_seq: int, *, at: dt.datetime) -> int:
        """Acknowledge everything at or below `attempt_seq` and return the cursor.

        Spool rows at or below the cursor are deleted; rows above it stay queued.
        The cursor only ever moves forward, so a late or duplicate ack cannot
        re-open already-acknowledged work.
        """
        conn = self._connection()
        with self._immediate(conn):
            conn.execute(
                "DELETE FROM spool WHERE attempt_id = ? AND attempt_seq <= ?",
                (attempt_id, attempt_seq),
            )
            row = conn.execute(
                "SELECT acked_seq FROM ack_cursors WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            previous = 0 if row is None else int(row["acked_seq"])
            new_cursor = max(previous, attempt_seq)
            conn.execute(
                "INSERT INTO ack_cursors (attempt_id, acked_seq, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(attempt_id) DO UPDATE SET"
                " acked_seq = excluded.acked_seq, updated_at = excluded.updated_at",
                (attempt_id, new_cursor, _to_text(at)),
            )
            return new_cursor

    def ack_cursor(self, attempt_id: str) -> int:
        """Return the highest acknowledged sequence, or 0. Survives a restart."""
        conn = self._connection()
        row = conn.execute(
            "SELECT acked_seq FROM ack_cursors WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return 0 if row is None else int(row["acked_seq"])

    def ack_cursors(self) -> dict[str, int]:
        """Return every attempt's acknowledged sequence, keyed by attempt ID."""
        conn = self._connection()
        rows = conn.execute("SELECT attempt_id, acked_seq FROM ack_cursors").fetchall()
        return {str(row["attempt_id"]): int(row["acked_seq"]) for row in rows}

    def meta_get(self, key: str) -> str | None:
        """Return a stored metadata value, or `None`."""
        conn = self._connection()
        row = conn.execute("SELECT value FROM node_meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def meta_set(self, key: str, value: str) -> None:
        """Store a metadata value, replacing any previous value for the key."""
        conn = self._connection()
        with self._immediate(conn):
            conn.execute(
                "INSERT INTO node_meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )



# --------------------------------------------------------------------------
# Row -> record builders. `sqlite3.Row` values are `Any`; every field is
# coerced to its declared type here so the rest of the module is typed.
# --------------------------------------------------------------------------


def _execution_from_row(row: Any) -> ExecutionRecord:
    finished = row["finished_at"]
    return ExecutionRecord(
        attempt_id=str(row["attempt_id"]),
        request_id=str(row["request_id"]),
        epoch=int(row["epoch"]),
        request_digest=str(row["request_digest"]),
        state=str(row["state"]),
        upstream_calls=int(row["upstream_calls"]),
        started_at=_from_text(str(row["started_at"])),
        finished_at=None if finished is None else _from_text(str(finished)),
    )


def _spool_from_row(row: Any) -> SpoolRecord:
    return SpoolRecord(
        spool_id=int(row["spool_id"]),
        attempt_id=str(row["attempt_id"]),
        attempt_seq=int(row["attempt_seq"]),
        event_type=str(row["event_type"]),
        payload=_load_payload(str(row["payload_json"])),
        enqueued_at=_from_text(str(row["enqueued_at"])),
    )
