"""Schema v30 stores virtual edits and their playlist identity.

[if] schema v30 is applied [then] edit metadata is synced atomically, [else stop].
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.engine_core.store import schema as store_schema
from apps.shared.state import migrations_v30
from apps.shared.state import schema as state_schema

pytestmark = pytest.mark.requirement("LIBEDIT-9179")


def _ladder_to(path: Path, version: int) -> None:
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    for step_index in range(version):
        for statement in state_schema.MIGRATIONS[step_index]:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (?, 'then')",
            (step_index + 1,),
        )
    conn.commit()
    conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def test_v30_is_the_next_shared_and_engine_store_rung() -> None:
    assert state_schema.SCHEMA_VERSION == 32
    assert state_schema.MIGRATIONS[29] is migrations_v30._V30
    assert store_schema.SCHEMA_VERSION == 21


def test_a_v29_database_gains_edits_membership_identity_and_write_guards(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    _ladder_to(path, 29)
    conn = sqlite3.connect(str(path))
    try:
        assert "edit_id" not in _columns(conn, "playlist_memberships")
        assert state_schema.apply_migrations(conn) == state_schema.SCHEMA_VERSION
        assert {
            "id",
            "source_track_id",
            "label",
            "in_ms",
            "out_ms",
            "fx",
            "created_by",
            "created_at",
            "updated_at",
            "origin_device_id",
            "deleted_at",
        } == _columns(conn, "track_edit")
        assert "edit_id" in _columns(conn, "playlist_memberships")
        assert conn.execute("SELECT 1 FROM sync_write_tokens WHERE table_name = 'track_edit'").fetchone()
        triggers = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert set(migrations_v30.EXPECTED_TRIGGERS) <= triggers
        assert state_schema.apply_migrations(conn) == state_schema.SCHEMA_VERSION
    finally:
        conn.close()


def test_a_failed_v30_step_rolls_back_to_v29(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _ladder_to(path, 29)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE track_edit(id TEXT PRIMARY KEY)")
    conn.commit()
    try:
        with pytest.raises(sqlite3.OperationalError):
            state_schema.apply_migrations(conn)
        assert conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] == 29
        assert "edit_id" not in _columns(conn, "playlist_memberships")
    finally:
        conn.close()


def test_the_fresh_engine_store_contains_the_edit_shape() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        store_schema.apply_migrations(conn)
        assert "source_track_id" in _columns(conn, "track_edit")
        assert "edit_id" in _columns(conn, "playlist_memberships")
        assert "track_edit" in store_schema.TABLES["library_org"]
    finally:
        conn.close()
