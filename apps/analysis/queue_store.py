"""Persistence for the native-analysis v1 backfill queue.

Spec `specs/native-analysis-v1.md` section 3 ("Queue") and section 4, lane
brief `specs/native-analysis-v1-lanes/nav1-queue.md`. Requirement NATIVE-10.

The queue is PERSISTED, not derived. :mod:`apps.analysis.backlog` is the
derived queue and stays that way: it answers "which unmapped tracks have no
analysis row" as a projection, which is exactly right for a question with no
state of its own. This queue is a different question. It has to survive a
cancel, a resume and a process KILL, has to say which items were already
complete before the kill, and has to be able to distinguish an item nobody
has started from one a dead worker was holding. None of that is derivable
from ``tracks`` + ``analysis``, so it is three tables:

* ``analysis_queue_batch`` -- one enqueue call: its admission decision (the
  worker count and band the memory rule chose), and its lifecycle state.
* ``analysis_queue_item`` -- one (track, lane) of work, with the state
  machine below, the predicted peak it was admitted at, and the runner that
  currently holds it.
* ``analysis_stale`` -- records whose DEPENDENCY moved underneath them.
  A key record computed against beatgrid v1 is not wrong in itself, but it
  no longer describes the canonical beatgrid, so it drops out of the
  canonical pointer until it is recomputed. Consulted by
  :func:`apps.analysis.canonical._eligible_rows`.

Item state machine, and what each state promises:

    pending    -> nobody holds it; a runner may claim it
    running    -> a named runner claimed it and has not committed yet.
                  A fresh process finds these and returns them to pending:
                  the work was never committed, so re-running it is exactly
                  once, not twice (the record write is idempotent on
                  (stable_id, backend, backend_version)).
    done       -> the record is committed. A fresh process never re-runs it
                  and never counts it twice.
    skipped    -> a current-version record already existed at claim time.
                  Terminal and idempotent: this is what makes a re-enqueue
                  of an already-analyzed library a no-op rather than a
                  duplicate write.
    failed     -> the backend declared the FILE the problem, with its reason.
    refused    -> the memory admission rule refused it, with its reason.
    cancelled  -> a cancel reached it while it was still pending.

The claim is a single UPDATE guarded on the current state, inside an
IMMEDIATE transaction, so two runners racing for the same item cannot both
win: the loser's UPDATE matches zero rows and it moves on.

-Claude
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .queue_stale import (
    STALE_DEPENDENCY_MOVED,
    STALE_TABLES_SQL,
    clear_stale,
    mark_stale,
    stale_rows,
)

#: Item states. Terminal states are the ones a resume never revisits.
ITEM_PENDING: str = "pending"
ITEM_RUNNING: str = "running"
ITEM_DONE: str = "done"
ITEM_SKIPPED: str = "skipped"
ITEM_FAILED: str = "failed"
ITEM_REFUSED: str = "refused"
ITEM_CANCELLED: str = "cancelled"
ITEM_STATES: tuple[str, ...] = (
    ITEM_PENDING,
    ITEM_RUNNING,
    ITEM_DONE,
    ITEM_SKIPPED,
    ITEM_FAILED,
    ITEM_REFUSED,
    ITEM_CANCELLED,
)
#: Reached the end of the road: a resume must not touch these.
TERMINAL_ITEM_STATES: tuple[str, ...] = (
    ITEM_DONE,
    ITEM_SKIPPED,
    ITEM_FAILED,
    ITEM_REFUSED,
)

#: Batch states.
BATCH_QUEUED: str = "queued"
BATCH_RUNNING: str = "running"
BATCH_CANCELLED: str = "cancelled"
BATCH_DONE: str = "done"
BATCH_STATES: tuple[str, ...] = (
    BATCH_QUEUED, BATCH_RUNNING, BATCH_CANCELLED, BATCH_DONE
)

#: Why an item was skipped without running.
SKIP_ALREADY_CURRENT: str = "already_current_version"

QUEUE_TABLES_SQL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS analysis_queue_batch (
        batch_id         TEXT PRIMARY KEY,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        state            TEXT NOT NULL,
        workers          INTEGER NOT NULL,
        band             TEXT NOT NULL,
        memory_model     TEXT NOT NULL,
        note             TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS analysis_queue_item (
        batch_id          TEXT NOT NULL,
        stable_id         TEXT NOT NULL,
        lane              TEXT NOT NULL,
        backend           TEXT NOT NULL,
        file_path         TEXT NOT NULL,
        duration_s        REAL,
        predicted_peak_mb REAL,
        state             TEXT NOT NULL,
        reason            TEXT,
        attempts          INTEGER NOT NULL DEFAULT 0,
        enqueued_at       TEXT NOT NULL,
        started_at        TEXT,
        finished_at       TEXT,
        runner_id         TEXT,
        PRIMARY KEY (batch_id, stable_id, lane)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_analysis_queue_item_state "
    "ON analysis_queue_item(state)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_queue_item_track "
    "ON analysis_queue_item(stable_id, lane)",
]


def ensure_queue_tables(conn: sqlite3.Connection) -> None:
    """Provision the queue schema, staleness table included. Idempotent."""
    for sql in (*QUEUE_TABLES_SQL, *STALE_TABLES_SQL):
        conn.execute(sql)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def new_batch_id() -> str:
    return "qb_" + uuid.uuid4().hex[:16]


def new_runner_id() -> str:
    return "qr_" + uuid.uuid4().hex[:12]


#-----------------------------------------------------------------------------
# rows
#-----------------------------------------------------------------------------

@dataclass(frozen=True)
class QueueItem:
    """One (track, lane) of queued work."""

    batch_id: str
    stable_id: str
    lane: str
    backend: str
    file_path: str
    duration_s: float | None
    predicted_peak_mb: float | None
    state: str
    reason: str | None
    attempts: int
    enqueued_at: str
    started_at: str | None
    finished_at: str | None
    runner_id: str | None


@dataclass(frozen=True)
class QueueBatch:
    """One enqueue call and the admission decision it was planned under."""

    batch_id: str
    created_at: str
    updated_at: str
    state: str
    workers: int
    band: str
    memory_model: dict[str, Any]
    note: str | None


_ITEM_COLUMNS = (
    "batch_id, stable_id, lane, backend, file_path, duration_s, "
    "predicted_peak_mb, state, reason, attempts, enqueued_at, started_at, "
    "finished_at, runner_id"
)


def _item_from_row(row: Sequence[Any]) -> QueueItem:
    return QueueItem(
        batch_id=row[0],
        stable_id=row[1],
        lane=row[2],
        backend=row[3],
        file_path=row[4],
        duration_s=row[5],
        predicted_peak_mb=row[6],
        state=row[7],
        reason=row[8],
        attempts=row[9],
        enqueued_at=row[10],
        started_at=row[11],
        finished_at=row[12],
        runner_id=row[13],
    )


def _batch_from_row(row: Sequence[Any]) -> QueueBatch:
    return QueueBatch(
        batch_id=row[0],
        created_at=row[1],
        updated_at=row[2],
        state=row[3],
        workers=row[4],
        band=row[5],
        memory_model=json.loads(row[6]),
        note=row[7],
    )


#-----------------------------------------------------------------------------
# batch
#-----------------------------------------------------------------------------

def create_batch(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    workers: int,
    band: str,
    memory_model: dict[str, Any],
    note: str | None = None,
) -> None:
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO analysis_queue_batch
            (batch_id, created_at, updated_at, state, workers, band,
             memory_model, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id, now, now, BATCH_QUEUED, int(workers), band,
            json.dumps(memory_model, sort_keys=True), note,
        ),
    )


def get_batch(conn: sqlite3.Connection, batch_id: str) -> QueueBatch | None:
    row = conn.execute(
        "SELECT batch_id, created_at, updated_at, state, workers, band, "
        "memory_model, note FROM analysis_queue_batch WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    return _batch_from_row(row) if row is not None else None


def list_batches(conn: sqlite3.Connection, *, limit: int = 50) -> list[QueueBatch]:
    rows = conn.execute(
        "SELECT batch_id, created_at, updated_at, state, workers, band, "
        "memory_model, note FROM analysis_queue_batch "
        "ORDER BY created_at DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [_batch_from_row(r) for r in rows]


def set_batch_state(
    conn: sqlite3.Connection, batch_id: str, state: str
) -> None:
    if state not in BATCH_STATES:
        raise ValueError(f"unknown batch state {state!r}; states are {BATCH_STATES}")
    conn.execute(
        "UPDATE analysis_queue_batch SET state = ?, updated_at = ? "
        "WHERE batch_id = ?",
        (state, _now_iso(), batch_id),
    )


def set_batch_workers(
    conn: sqlite3.Connection, batch_id: str, *, workers: int, band: str
) -> None:
    """Re-plan a batch's concurrency after a resume changed the admitted set."""
    conn.execute(
        "UPDATE analysis_queue_batch SET workers = ?, band = ?, updated_at = ? "
        "WHERE batch_id = ?",
        (int(workers), band, _now_iso(), batch_id),
    )


#-----------------------------------------------------------------------------
# items
#-----------------------------------------------------------------------------

@dataclass(frozen=True)
class NewItem:
    """One row an enqueue is about to write.

    A dataclass rather than eight keyword arguments: the fields travel
    together from the admission plan to the row, and a positional mistake
    between two same-typed strings (``lane`` and ``backend``) is a bug no
    type checker would catch.
    """

    stable_id: str
    lane: str
    backend: str
    file_path: str
    duration_s: float | None
    predicted_peak_mb: float | None
    state: str
    reason: str | None = None


def add_item(conn: sqlite3.Connection, batch_id: str, item: NewItem) -> None:
    if item.state not in ITEM_STATES:
        raise ValueError(
            f"unknown item state {item.state!r}; states are {ITEM_STATES}"
        )
    now = _now_iso()
    settled = item.state in TERMINAL_ITEM_STATES or item.state == ITEM_CANCELLED
    conn.execute(
        f"""
        INSERT INTO analysis_queue_item ({_ITEM_COLUMNS})
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, NULL)
        """,
        (
            batch_id, item.stable_id, item.lane, item.backend, item.file_path,
            item.duration_s, item.predicted_peak_mb, item.state, item.reason,
            now, now if settled else None,
        ),
    )


def list_items(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    states: Iterable[str] | None = None,
    limit: int | None = None,
) -> list[QueueItem]:
    sql = f"SELECT {_ITEM_COLUMNS} FROM analysis_queue_item WHERE batch_id = ?"
    params: list[Any] = [batch_id]
    if states is not None:
        wanted = tuple(states)
        if not wanted:
            return []
        sql += f" AND state IN ({','.join('?' * len(wanted))})"
        params.extend(wanted)
    sql += " ORDER BY enqueued_at, stable_id, lane"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [_item_from_row(r) for r in conn.execute(sql, params).fetchall()]


def get_item(
    conn: sqlite3.Connection, batch_id: str, stable_id: str, lane: str
) -> QueueItem | None:
    row = conn.execute(
        f"SELECT {_ITEM_COLUMNS} FROM analysis_queue_item "
        "WHERE batch_id = ? AND stable_id = ? AND lane = ?",
        (batch_id, stable_id, lane),
    ).fetchone()
    return _item_from_row(row) if row is not None else None


def counts_by_state(conn: sqlite3.Connection, batch_id: str) -> dict[str, int]:
    """Every state's count, including the zeros.

    Zeros are present on purpose: a caller reading ``counts["failed"]`` must
    get 0 rather than a KeyError that a defensive ``.get(..., 0)`` would then
    turn into an unmeasured zero.
    """
    out = dict.fromkeys(ITEM_STATES, 0)
    for state, n in conn.execute(
        "SELECT state, COUNT(*) FROM analysis_queue_item WHERE batch_id = ? "
        "GROUP BY state",
        (batch_id,),
    ):
        out[state] = int(n)
    return out


def claim_next(
    conn: sqlite3.Connection, batch_id: str, *, runner_id: str
) -> QueueItem | None:
    """Atomically take the next pending item, or return None.

    IMMEDIATE so the SELECT and the UPDATE cannot be interleaved by another
    runner: SQLite's deferred transactions take the write lock only at the
    first write, which would leave exactly the window two runners need to
    claim the same row.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM analysis_queue_item "
            "WHERE batch_id = ? AND state = ? "
            "ORDER BY enqueued_at, stable_id, lane LIMIT 1",
            (batch_id, ITEM_PENDING),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        item = _item_from_row(row)
        now = _now_iso()
        changed = conn.execute(
            "UPDATE analysis_queue_item SET state = ?, started_at = ?, "
            "runner_id = ?, attempts = attempts + 1 "
            "WHERE batch_id = ? AND stable_id = ? AND lane = ? AND state = ?",
            (
                ITEM_RUNNING, now, runner_id, batch_id, item.stable_id,
                item.lane, ITEM_PENDING,
            ),
        ).rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if changed != 1:
        # Another runner won the row between the SELECT and the UPDATE.
        # Impossible under IMMEDIATE, so it is a real invariant break rather
        # than a race to retry quietly.
        raise RuntimeError(
            f"claim of {item.stable_id}/{item.lane} in {batch_id} updated "
            f"{changed} rows under an IMMEDIATE transaction"
        )
    return QueueItem(
        **{
            **item.__dict__,
            "state": ITEM_RUNNING,
            "started_at": now,
            "runner_id": runner_id,
            "attempts": item.attempts + 1,
        }
    )


def finish_item(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    stable_id: str,
    lane: str,
    state: str,
    reason: str | None = None,
    claimed_by: str | None = None,
) -> bool:
    """Move a claimed item to a terminal state. Caller owns the transaction.

    Deliberately takes no lock of its own: the runner calls this INSIDE the
    same transaction as the record write, so a kill can never land between
    "the record is committed" and "the item is done".

    ``claimed_by`` narrows the update to an item that is STILL ``running``
    under that runner id, and is how a live cancel survives a worker that
    finishes a moment later. Without it the update matches on
    (batch, stable_id, lane) alone, so `/cancel` moves an in-flight item to
    ``cancelled`` and the worker's own settlement moves it straight back to
    ``done`` -- cancellation that does not cancel, and an item ``resume``
    will never revisit because it reads as terminal.

    Returns whether a row moved. ``False`` means the claim was taken away
    (cancelled, or released to another runner) and the caller must discard
    the result rather than record it.
    """
    if state not in ITEM_STATES:
        raise ValueError(f"unknown item state {state!r}; states are {ITEM_STATES}")
    sql = (
        "UPDATE analysis_queue_item SET state = ?, reason = ?, finished_at = ?, "
        "runner_id = NULL WHERE batch_id = ? AND stable_id = ? AND lane = ?"
    )
    params: list[Any] = [state, reason, _now_iso(), batch_id, stable_id, lane]
    if claimed_by is not None:
        sql += " AND state = ? AND runner_id = ?"
        params += [ITEM_RUNNING, claimed_by]
    return conn.execute(sql, params).rowcount == 1


def release_running_items(conn: sqlite3.Connection, batch_id: str) -> int:
    """Return every ``running`` item of a batch to ``pending``.

    Called when a FRESH process takes over a batch. An item in ``running``
    was claimed by a runner that no longer exists, and its record was never
    committed (the commit and the state change are one transaction), so
    returning it to pending re-runs it exactly once rather than losing it.
    """
    return conn.execute(
        "UPDATE analysis_queue_item SET state = ?, runner_id = NULL, "
        "started_at = NULL WHERE batch_id = ? AND state = ?",
        (ITEM_PENDING, batch_id, ITEM_RUNNING),
    ).rowcount


def cancel_open_items(conn: sqlite3.Connection, batch_id: str) -> int:
    """Cancel everything not already terminal: pending AND in flight.

    An in-flight item goes to ``cancelled`` like a pending one, because its
    record was not committed. A resume moves both back to pending.
    """
    return conn.execute(
        "UPDATE analysis_queue_item SET state = ?, runner_id = NULL, "
        "finished_at = ? WHERE batch_id = ? AND state IN (?, ?)",
        (ITEM_CANCELLED, _now_iso(), batch_id, ITEM_PENDING, ITEM_RUNNING),
    ).rowcount


def revive_cancelled_items(conn: sqlite3.Connection, batch_id: str) -> int:
    """Put a cancelled batch's items back in the queue. Terminal ones stay."""
    return conn.execute(
        "UPDATE analysis_queue_item SET state = ?, finished_at = NULL, "
        "started_at = NULL, runner_id = NULL "
        "WHERE batch_id = ? AND state = ?",
        (ITEM_PENDING, batch_id, ITEM_CANCELLED),
    ).rowcount


__all__ = [
    "BATCH_CANCELLED",
    "BATCH_DONE",
    "BATCH_QUEUED",
    "BATCH_RUNNING",
    "BATCH_STATES",
    "ITEM_CANCELLED",
    "ITEM_DONE",
    "ITEM_FAILED",
    "ITEM_PENDING",
    "ITEM_REFUSED",
    "ITEM_RUNNING",
    "ITEM_SKIPPED",
    "ITEM_STATES",
    "QUEUE_TABLES_SQL",
    "SKIP_ALREADY_CURRENT",
    "STALE_DEPENDENCY_MOVED",
    "STALE_TABLES_SQL",
    "TERMINAL_ITEM_STATES",
    "NewItem",
    "QueueBatch",
    "QueueItem",
    "add_item",
    "cancel_open_items",
    "claim_next",
    "clear_stale",
    "counts_by_state",
    "create_batch",
    "ensure_queue_tables",
    "finish_item",
    "get_batch",
    "get_item",
    "list_batches",
    "list_items",
    "mark_stale",
    "new_batch_id",
    "new_runner_id",
    "release_running_items",
    "revive_cancelled_items",
    "set_batch_state",
    "set_batch_workers",
    "stale_rows",
]
