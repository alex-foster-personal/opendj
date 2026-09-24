"""Migration v18: persisted path availability index (issue #1037, PERF-RB-01).

[if] v18 is applied [then] path_availability and its checked_at index exist, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema

pytestmark = pytest.mark.requirement("PERF-RB-01")


def _objects(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        )
    }


def test_fresh_ladder_creates_path_availability() -> None:
    """[if] a fresh schema is applied [then] the index table exists, [else stop]."""
    conn = sqlite3.connect(":memory:")
    state_schema.apply_migrations(conn)
    assert {"path_availability", "idx_path_availability_checked"} <= _objects(conn)
    conn.close()


def test_v17_store_upgrades_to_v18(tmp_path: Path) -> None:
    """[if] a v17 db opens after v18 lands [then] the table appears, [else stop]."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    try:
        for step_idx in range(17):
            for stmt in state_schema.MIGRATIONS[step_idx]:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
                (step_idx + 1, "2026-09-01T00:00:00+00:00"),
            )
        conn.commit()
        assert "path_availability" not in _objects(conn)  # negative control
    finally:
        conn.close()

    state_db.open_rw(path).close()
    upgraded = sqlite3.connect(str(path))
    try:
        assert "path_availability" in _objects(upgraded)
        version = upgraded.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
        assert version == state_schema.SCHEMA_VERSION >= 18
    finally:
        upgraded.close()
