"""Migration v29: index live playlist memberships by stable_id (issue #8785)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from apps.engine_core.store import schema as store_schema
from apps.shared.state import db as state_db
from apps.shared.state import migrations_v29
from apps.shared.state import schema as state_schema
from apps.webui.server.inbox_playlist import _UNFILED_SQL

_INDEX_NAME = "idx_playlist_memberships_live_by_stable"


def _ladder_to(path: Path, version: int) -> None:
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    for step_idx in range(version):
        for stmt in state_schema.MIGRATIONS[step_idx]:
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_meta(version, applied_at) VALUES (?, 'then')", (step_idx + 1,))
    conn.commit()
    conn.close()


def _has_index(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (_INDEX_NAME,),
        ).fetchone()
        is not None
    )


def _seed_query_rows(path: Path) -> None:
    _ladder_to(path, state_schema.SCHEMA_VERSION)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, created_at, updated_at) "
            "VALUES ('pl-live', 'Live', 'rekordbox', 'RB-1', 't', 't')"
        )
        for index in range(8):
            stable_id = f"sid-{index}"
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, album, isrc, "
                "duration_ms, file_path, content_hash, created_at, updated_at) "
                "VALUES (?, 'inferred', ?, '[]', ?, NULL, 100000, ?, NULL, 't', 't')",
                (stable_id, stable_id, stable_id, f"/music/{stable_id}.mp3"),
            )
            if index:
                conn.execute(
                    "INSERT INTO playlist_memberships(playlist_id, stable_id, position) VALUES (?, ?, ?)",
                    ("pl-live", stable_id, index),
                )
        conn.commit()
    finally:
        conn.close()


def _query_plan(conn: sqlite3.Connection) -> str:
    return " | ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + _UNFILED_SQL))


def test_v29_step_matches_the_ladder_module() -> None:
    assert state_schema.MIGRATIONS[28] is migrations_v29._V29


def test_v28_db_gains_the_live_by_stable_index_on_open(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _ladder_to(path, 28)
    before = sqlite3.connect(str(path))
    try:
        assert not _has_index(before)
    finally:
        before.close()

    opened = state_db.open_rw(path)
    try:
        assert state_schema.apply_migrations(opened) == state_schema.SCHEMA_VERSION
        assert _has_index(opened)
    finally:
        opened.close()


def test_second_open_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    state_db.open_rw(path).close()
    again = state_db.open_rw(path)
    try:
        assert state_schema.apply_migrations(again) == state_schema.SCHEMA_VERSION
        assert _has_index(again)
    finally:
        again.close()


def test_fresh_databases_expose_the_index(tmp_path: Path) -> None:
    legacy = state_db.open_rw(tmp_path / "legacy.db")
    try:
        assert _has_index(legacy)
    finally:
        legacy.close()

    consolidated = sqlite3.connect(":memory:")
    store_schema.apply_migrations(consolidated)
    try:
        assert _has_index(consolidated)
    finally:
        consolidated.close()


def test_unfiled_query_searches_the_new_index(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _seed_query_rows(path)
    conn = sqlite3.connect(str(path))
    try:
        plan = _query_plan(conn)
        assert "SEARCH m USING INDEX idx_playlist_memberships_live_by_stable (stable_id=?)" in plan
        assert "SCAN m" not in plan
    finally:
        conn.close()


def test_dropping_the_index_returns_the_subquery_to_a_scan(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _seed_query_rows(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"DROP INDEX {_INDEX_NAME}")
        plan = _query_plan(conn)
        assert "SCAN m" in plan
        assert _INDEX_NAME not in plan
    finally:
        conn.close()
