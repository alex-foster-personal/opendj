"""The staleness table: records whose DEPENDENCY moved underneath them.

Split out of :mod:`apps.analysis.queue_store` (600-line file ratchet), and it
is a clean seam rather than an arbitrary cut: this table is the ONE piece of
queue state the canonical pointer reads. :mod:`apps.analysis.canonical`
imports it directly and nothing else from the queue.

A key record computed against beatgrid v1 is not wrong in itself, but once the
canonical beatgrid moves it no longer describes the grid the deck reads. So it
drops out of ``analysis_canonical`` until it is recomputed, WITHOUT the row
being deleted: rows never overwrite across producers, and produced work is not
thrown away because its dependency moved.

-Claude
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

#: Why a record went stale.
STALE_DEPENDENCY_MOVED: str = "dependency_record_changed"

STALE_TABLES_SQL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS analysis_stale (
        stable_id        TEXT NOT NULL,
        lane             TEXT NOT NULL,
        backend          TEXT NOT NULL,
        backend_version  TEXT NOT NULL,
        reason           TEXT NOT NULL,
        detected_at      TEXT NOT NULL,
        PRIMARY KEY (stable_id, lane, backend, backend_version)
    )
    """,
]


def ensure_stale_table(conn: sqlite3.Connection) -> None:
    """Provision the staleness table. Idempotent."""
    for sql in STALE_TABLES_SQL:
        conn.execute(sql)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


#-----------------------------------------------------------------------------
# staleness
#-----------------------------------------------------------------------------

def mark_stale(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    lane: str,
    backend: str,
    backend_version: str,
    reason: str,
) -> None:
    conn.execute(
        """
        INSERT INTO analysis_stale
            (stable_id, lane, backend, backend_version, reason, detected_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(stable_id, lane, backend, backend_version) DO UPDATE SET
            reason = excluded.reason, detected_at = excluded.detected_at
        """,
        (stable_id, lane, backend, backend_version, reason, _now_iso()),
    )


def clear_stale(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    lane: str,
    backend: str | None = None,
    backend_version: str | None = None,
) -> int:
    sql = "DELETE FROM analysis_stale WHERE stable_id = ? AND lane = ?"
    params: list[Any] = [stable_id, lane]
    if backend is not None:
        sql += " AND backend = ?"
        params.append(backend)
    if backend_version is not None:
        sql += " AND backend_version = ?"
        params.append(backend_version)
    return conn.execute(sql, params).rowcount


def stale_rows(
    conn: sqlite3.Connection, stable_id: str, lane: str
) -> set[tuple[str, str]]:
    """``(backend, backend_version)`` pairs excluded from the canonical pointer."""
    return {
        (row[0], row[1])
        for row in conn.execute(
            "SELECT backend, backend_version FROM analysis_stale "
            "WHERE stable_id = ? AND lane = ?",
            (stable_id, lane),
        )
    }

__all__ = [
    "STALE_DEPENDENCY_MOVED",
    "STALE_TABLES_SQL",
    "clear_stale",
    "ensure_stale_table",
    "mark_stale",
    "stale_rows",
]
