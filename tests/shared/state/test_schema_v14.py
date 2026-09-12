"""Migration v14: per-playlist forbid_duplicates (LIBM-D2 / #2416).

[if] schema v14 is applied [then] playlists gain forbid_duplicates defaulting to 0, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import schema as state_schema
from apps.shared.state.migrations_v14 import _V14

pytestmark = [pytest.mark.requirement("INFRA-01"), pytest.mark.requirement("LIBM-04b")]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def test_fresh_ladder_has_forbid_duplicates() -> None:
    """[if] a fresh schema is applied [then] playlists have forbid_duplicates=0, [else stop]."""
    conn = sqlite3.connect(":memory:")
    state_schema.apply_migrations(conn)
    cols = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(playlists)")}
    assert cols["forbid_duplicates"] == "INTEGER"
    count = conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0]
    if count:
        assert conn.execute(
            "SELECT COUNT(*) FROM playlists WHERE forbid_duplicates != 0"
        ).fetchone()[0] == 0
    conn.close()


def test_v13_db_migrates_forbid_duplicates(tmp_path: Path) -> None:
    """[if] a v13 database is migrated [then] existing playlists keep forbid_duplicates=0, [else stop]."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    try:
        for step_idx in range(13):
            for stmt in state_schema.MIGRATIONS[step_idx]:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
                (step_idx + 1, "2026-09-01T00:00:00+00:00"),
            )
        conn.execute(
            "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, "
            "created_at, updated_at) VALUES ('pl-1', 'Test', 'webui', 'v1', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(str(path))
    try:
        for stmt in _V14:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (14, ?)",
            ("2026-09-13T00:00:00+00:00",),
        )
        conn.commit()
        row = conn.execute(
            "SELECT forbid_duplicates FROM playlists WHERE playlist_id='pl-1'"
        ).fetchone()
        assert row is not None
        assert row[0] == 0
    finally:
        conn.close()

    fresh = sqlite3.connect(":memory:")
    state_schema.apply_migrations(fresh)
    migrated = sqlite3.connect(str(path))
    assert _columns(migrated, "playlists") == _columns(fresh, "playlists")
    migrated.close()
    fresh.close()
