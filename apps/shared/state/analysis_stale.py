"""The staleness table: records whose DEPENDENCY moved underneath them.

Split out of :mod:`apps.analysis.queue_store` (600-line file ratchet), and it
is a clean seam rather than an arbitrary cut: this table is the ONE piece of
queue state the canonical pointer reads. :mod:`apps.analysis.canonical`
imports it directly and nothing else from the queue.

It lives in the shared state layer because folder ingest marks records stale
when a file's bytes are replaced, and ``apps.shared`` may import no sibling
package. Supersedes: ``apps/analysis/queue_stale.py``.

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
#: The file was rewritten and the record cannot be remade from the new bytes.
STALE_AUDIO_REPLACED: str = "audio_bytes_replaced"
#: The ``lane`` of a marker that withholds a whole record rather than one lane:
#: a legacy record has no lanes, and the latest-record readers serve records
#: whole. No lane has this name, so the canonical pointer never reads it.
WHOLE_RECORD: str = "*"

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

def retired_rows(conn: sqlite3.Connection, backend: str) -> set[tuple[str, str]]:
    """``(stable_id, backend_version)`` of ``backend`` rows a whole-record marker withholds.

    These describe bytes the file no longer holds (folder ingest kept them for
    history), so a "has this track been analyzed" check must not count them.
    """
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'analysis_stale'"
    ).fetchone() is None:
        return set()
    return {
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT stable_id, backend_version FROM analysis_stale WHERE lane = ? AND backend = ?",
            (WHOLE_RECORD, backend),
        )
    }

__all__ = [
    "STALE_AUDIO_REPLACED",
    "STALE_DEPENDENCY_MOVED",
    "STALE_TABLES_SQL",
    "WHOLE_RECORD",
    "clear_stale",
    "ensure_stale_table",
    "mark_stale",
    "retired_rows",
    "stale_rows",
]
