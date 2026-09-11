"""Build a schema-v6 state.db (migrations 1..6, no ``deleted_at``)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from apps.shared.state import schema as state_schema

_TS = "2026-04-17T10:00:00.000000Z"
TRACK_STABLE_ID = "sid-old"
PLAYLIST_ID = "pl-old"
PLAYLIST_NAME = "Legacy Set"
TRACK_FILE_PATH = "/music/old.mp3"


def build_pre_v7_state_db(path: Path) -> Path:
    """Replay ``MIGRATIONS[0..5]`` and seed one track + playlist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("BEGIN")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for step_idx in range(6):
            for stmt in state_schema.MIGRATIONS[step_idx]:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
                (step_idx + 1, _TS),
            )
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
            "album, isrc, duration_ms, file_path, content_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                TRACK_STABLE_ID,
                "inferred",
                "Time Traveler",
                "[]",
                None,
                None,
                None,
                TRACK_FILE_PATH,
                None,
                _TS,
                _TS,
            ),
        )
        conn.execute(
            "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (PLAYLIST_ID, PLAYLIST_NAME, "rekordbox", "rb-pl-1", _TS, _TS),
        )
        conn.execute(
            "INSERT INTO playlist_memberships (playlist_id, stable_id, position) "
            "VALUES (?, ?, ?)",
            (PLAYLIST_ID, TRACK_STABLE_ID, 0),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return path


def assert_no_deleted_at_column(conn: sqlite3.Connection, table: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    assert "deleted_at" not in columns
