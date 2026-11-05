"""Set target and skip flags, stored in state.db next to the playlist they belong to.

Two side tables keyed by ``playlist_id`` (the playlist the browser shows as a
set): ``set_targets`` holds the optional target length, ``set_skips`` the
stable ids marked "skip in set". The playlist row itself is event-sourced
(``PlaylistStore``), so these flags live beside it, like ``playlist_sets``.

-Claude
"""

from __future__ import annotations

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS set_targets (
    playlist_id TEXT PRIMARY KEY,
    target_s REAL NOT NULL CHECK (target_s > 0)
);
CREATE TABLE IF NOT EXISTS set_skips (
    playlist_id TEXT NOT NULL,
    stable_id TEXT NOT NULL,
    PRIMARY KEY (playlist_id, stable_id)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def get_target_s(conn: sqlite3.Connection, playlist_id: str) -> float | None:
    row = conn.execute("SELECT target_s FROM set_targets WHERE playlist_id = ?", (playlist_id,)).fetchone()
    return None if row is None else float(row[0])


def set_target_s(conn: sqlite3.Connection, playlist_id: str, target_s: float | None) -> None:
    """Store the target; ``None`` clears it."""
    if target_s is None:
        conn.execute("DELETE FROM set_targets WHERE playlist_id = ?", (playlist_id,))
    elif target_s > 0:
        conn.execute(
            "INSERT INTO set_targets (playlist_id, target_s) VALUES (?, ?) "
            "ON CONFLICT(playlist_id) DO UPDATE SET target_s = excluded.target_s",
            (playlist_id, float(target_s)),
        )
    else:
        raise ValueError(f"target_s must be positive, got {target_s}")


def get_skipped(conn: sqlite3.Connection, playlist_id: str) -> set[str]:
    rows = conn.execute("SELECT stable_id FROM set_skips WHERE playlist_id = ?", (playlist_id,)).fetchall()
    return {str(r[0]) for r in rows}


def set_skipped(conn: sqlite3.Connection, playlist_id: str, stable_id: str, skipped: bool) -> None:
    if skipped:
        conn.execute("INSERT OR IGNORE INTO set_skips (playlist_id, stable_id) VALUES (?, ?)",
                     (playlist_id, stable_id))
    else:
        conn.execute("DELETE FROM set_skips WHERE playlist_id = ? AND stable_id = ?", (playlist_id, stable_id))
