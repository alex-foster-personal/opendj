"""Migration v25: transition memory (MEMORY-01).

[if] a v24 state.db is opened [then] pair_transitions exists and its version is stamped, [else stop].
[if] a row names an unknown source [then] sqlite refuses it, [else stop].
[if] the engine store migrates [then] it holds the same table, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.engine_core.store import schema as store_schema
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema

pytestmark = pytest.mark.requirement("MEMORY-01")


def _ladder_to(path: Path, version: int) -> None:
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    for step_idx in range(version):
        for stmt in state_schema.MIGRATIONS[step_idx]:
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_meta(version, applied_at) VALUES (?, 'then')", (step_idx + 1,))
    conn.commit()
    conn.close()


def _columns(conn: sqlite3.Connection) -> list[str]:
    return [str(row[1]) for row in conn.execute("PRAGMA table_info(pair_transitions)")]


def _insert(conn: sqlite3.Connection, source: str) -> None:
    conn.execute(
        "INSERT INTO pair_transitions (out_track_id, in_track_id, plan_json, source, style, created_at, updated_at) "
        "VALUES ('a', 'b', '{}', ?, 'captured', 'now', 'now')",
        (source,),
    )


def test_a_v24_db_gains_the_table_on_open(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _ladder_to(path, 24)
    before = sqlite3.connect(str(path))
    assert _columns(before) == []  # the negative control: v24 does not have it
    before.close()
    conn = state_db.open_rw(path)
    try:
        state_schema.apply_migrations(conn)
        assert conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] == state_schema.SCHEMA_VERSION
        assert _columns(conn) == [
            "out_track_id",
            "in_track_id",
            "plan_json",
            "source",
            "style",
            "created_at",
            "updated_at",
            "times_played",
        ]
    finally:
        conn.close()


def test_one_row_per_ordered_pair_and_sources_are_checked(tmp_path: Path) -> None:
    conn = state_db.open_rw(tmp_path / "state.db")
    try:
        state_schema.apply_migrations(conn)
        _insert(conn, "captured")
        assert conn.execute("SELECT times_played FROM pair_transitions").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, "planner")  # the same ordered pair
        conn.execute("DELETE FROM pair_transitions")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, "guessed")
    finally:
        conn.close()


def test_the_engine_store_ladder_carries_the_same_table() -> None:
    assert "pair_transitions" in store_schema.TABLES["pair_transitions"]
    assert any("CREATE TABLE IF NOT EXISTS pair_transitions" in statement for statement in store_schema.ALL_DDL)
    assert all("IF NOT EXISTS" in statement for statement in store_schema._V14)
