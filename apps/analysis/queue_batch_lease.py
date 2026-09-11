"""Exclusive batch-runner lease for the backfill queue drain.

Split out of :mod:`apps.analysis.queue_store` when the batch-lease columns
and takeover guard pushed that module past the 600-line ratchet (PR #1586).

-Claude
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
from datetime import UTC, datetime

from . import queue_store as qs


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def ensure_batch_lease_columns(conn: sqlite3.Connection) -> None:
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(analysis_queue_batch)")
    }
    if "active_runner_id" not in cols:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(
                "ALTER TABLE analysis_queue_batch ADD COLUMN active_runner_id TEXT"
            )
    if "active_runner_pid" not in cols:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(
                "ALTER TABLE analysis_queue_batch ADD COLUMN active_runner_pid INTEGER"
            )


def pid_is_alive(pid: int) -> bool:
    """Whether ``pid`` still exists. Used by the runner's takeover guard."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _refuse_if_live_runner_holds_batch(
    *,
    batch_id: str,
    state: str,
    active_pid: int | None,
    runner_pid: int,
) -> None:
    if (
        state == qs.BATCH_RUNNING
        and active_pid is not None
        and pid_is_alive(int(active_pid))
        and int(active_pid) != runner_pid
    ):
        raise RuntimeError(
            f"batch {batch_id!r} is already being drained by live runner "
            f"pid {active_pid}; refusing a second concurrent pool"
        )


def _require_batch_row(
    row: tuple[str, int | None] | None, batch_id: str
) -> tuple[str, int | None]:
    if row is None:
        raise ValueError(f"no such batch {batch_id!r}")
    return row


def take_batch_runner(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    runner_id: str,
    runner_pid: int,
) -> int:
    """Exclusive batch lease for one drain process."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT state, active_runner_pid FROM analysis_queue_batch "
            "WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        state, active_pid = _require_batch_row(row, batch_id)
        _refuse_if_live_runner_holds_batch(
            batch_id=batch_id,
            state=state,
            active_pid=active_pid,
            runner_pid=runner_pid,
        )
        released = qs.release_running_items(conn, batch_id)
        now = _now_iso()
        conn.execute(
            "UPDATE analysis_queue_batch SET state = ?, updated_at = ?, "
            "active_runner_id = ?, active_runner_pid = ? WHERE batch_id = ?",
            (qs.BATCH_RUNNING, now, runner_id, runner_pid, batch_id),
        )
        conn.execute("COMMIT")
    except RuntimeError:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return released


def clear_batch_runner(conn: sqlite3.Connection, batch_id: str) -> None:
    conn.execute(
        "UPDATE analysis_queue_batch SET active_runner_id = NULL, "
        "active_runner_pid = NULL, updated_at = ? WHERE batch_id = ?",
        (_now_iso(), batch_id),
    )


__all__ = [
    "clear_batch_runner",
    "ensure_batch_lease_columns",
    "pid_is_alive",
    "take_batch_runner",
]
