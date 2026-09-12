"""Migration v13: addressable playlist membership rows (LIBM-20)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.engine_core.store.schema import normalize_object_sql
from apps.shared.state import schema as state_schema
from apps.shared.state.migrations_v13 import _V13

pytestmark = [pytest.mark.requirement("INFRA-01"), pytest.mark.requirement("LIBM-20")]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _objects(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(name): normalize_object_sql(str(sql))
        for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index')"
        ).fetchall()
        if sql is not None and not str(name).startswith("sqlite_")
    }


def test_fresh_ladder_has_item_id_and_order_key() -> None:
    conn = sqlite3.connect(":memory:")
    state_schema.apply_migrations(conn)
    cols = _columns(conn, "playlist_memberships")
    assert "item_id" in cols
    assert "order_key" in cols
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='playlist_memberships'"
        )
    }
    assert "idx_playlist_memberships_item_id" in indexes
    conn.close()


def test_v12_db_migrates_memberships_backfill(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    try:
        for step_idx in range(12):
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
        for sid in ["t-a", "t-b", "t-c"]:
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, title, "
                "created_at, updated_at) "
                "VALUES (?, 'inferred', ?, '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z')",
                (sid, sid),
            )
        for pos, sid in enumerate(["t-a", "t-b", "t-c"]):
            conn.execute(
                "INSERT INTO playlist_memberships(playlist_id, stable_id, position, "
                "updated_at) VALUES ('pl-1', ?, ?, '2026-01-01T00:00:00Z')",
                (sid, pos),
            )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(str(path))
    try:
        for stmt in _V13:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (13, ?)",
            ("2026-09-12T00:00:00+00:00",),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT stable_id, position, item_id, order_key "
            "FROM playlist_memberships WHERE playlist_id='pl-1' ORDER BY position"
        ).fetchall()
        assert len(rows) == 3
        item_ids = [r[2] for r in rows]
        assert len(set(item_ids)) == 3
        assert all(iid is not None for iid in item_ids)
        for stable_id, position, _iid, order_key in rows:
            assert order_key == f"{position:08d}"
        assert [r[0] for r in rows] == ["t-a", "t-b", "t-c"]
    finally:
        conn.close()

    fresh = sqlite3.connect(":memory:")
    state_schema.apply_migrations(fresh)
    migrated = sqlite3.connect(str(path))
    assert _columns(migrated, "playlist_memberships") == _columns(
        fresh, "playlist_memberships",
    )
    migrated.close()
    fresh.close()
