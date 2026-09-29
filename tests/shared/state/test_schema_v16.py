"""Migration v16: backfill markers and changelog indexes (#3165).

[if] schema v16 is applied [then] marker table and indexes exist, [else stop].
[if] changelog membership is probed [then] SQLite uses the composite index, [else stop].
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.shared.state import sync_stamp
from apps.shared.state.migrations_v15 import (
    MARKER_TABLE,
    TRACK_FIELDS_STAMP_BACKFILL_MARKER,
    _changelog_has_row,
)

pytestmark = [pytest.mark.requirement("INFRA-01"), pytest.mark.requirement("CLOUDSYNC-03")]

_T_STAMP = "2026-08-30T10:00:00.000000+00:00"


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    return {str(row[1]) for row in rows}


def _explain_uses_index(
    conn: sqlite3.Connection, changelog_table: str, encoded_pk: str
) -> bool:
    plan = conn.execute(
        f"EXPLAIN QUERY PLAN SELECT 1 FROM {changelog_table} "
        "WHERE table_name = ? AND row_pk = ? LIMIT 1",
        ("track_fields", encoded_pk),
    ).fetchall()
    detail = " ".join(str(cell) for row in plan for cell in row).upper()
    uses_index = "USING INDEX" in detail or "USING COVERING INDEX" in detail
    return uses_index and f"SCAN {changelog_table.upper()}" not in detail


def test_fresh_ladder_reaches_v16() -> None:
    """[if] a fresh schema is applied [then] schema_meta records v16, [else stop]."""
    conn = sqlite3.connect(":memory:")
    state_schema.apply_migrations(conn)
    version = conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
    assert version == state_schema.SCHEMA_VERSION
    assert _index_names(conn, "hub_changelog") >= {"idx_hub_changelog_table"}
    assert _index_names(conn, "local_changelog") >= {"idx_local_changelog_table"}
    conn.close()


def test_v15_to_v16_upgrade_adds_marker_table_and_hub_index(tmp_path: Path) -> None:
    """[if] a v15 DB upgrades [then] marker table and hub index appear, [else stop]."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    for step_idx in range(14):
        for stmt in state_schema.MIGRATIONS[step_idx]:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
            (step_idx + 1, "2026-09-01T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()

    state_db.open_rw(path).close()

    upgraded = sqlite3.connect(str(path))
    try:
        assert upgraded.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (MARKER_TABLE,),
        ).fetchone() is not None
        assert "idx_hub_changelog_table" in _index_names(upgraded, "hub_changelog")
        marker = upgraded.execute(
            f"SELECT marker FROM {MARKER_TABLE} WHERE marker = ?",
            (TRACK_FIELDS_STAMP_BACKFILL_MARKER,),
        ).fetchone()
        assert marker is not None
    finally:
        upgraded.close()


def test_changelog_has_row_uses_composite_index(tmp_path: Path) -> None:
    """[if] changelog membership is probed [then] EXPLAIN reports index search, [else stop]."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    state_schema.apply_migrations(conn)
    encoded = sync_stamp.encode_row_pk(("trk-1", "bpm"))
    conn.execute(
        "INSERT INTO local_changelog(table_name, row_pk, updated_at, "
        "origin_device_id, received_at) VALUES (?, ?, ?, ?, ?)",
        ("track_fields", encoded, _T_STAMP, "dev-a", _T_STAMP),
    )
    conn.commit()
    assert _changelog_has_row(conn, "local_changelog", "track_fields", encoded)
    assert _explain_uses_index(conn, "local_changelog", encoded)
    conn.execute(
        "INSERT INTO hub_changelog(table_name, row_pk, updated_at, "
        "origin_device_id, received_at) VALUES (?, ?, ?, ?, ?)",
        ("track_fields", encoded, _T_STAMP, "dev-a", _T_STAMP),
    )
    conn.commit()
    assert _changelog_has_row(conn, "hub_changelog", "track_fields", encoded)
    assert _explain_uses_index(conn, "hub_changelog", encoded)
    conn.close()


def _seed_large_backfill_fixture(conn: sqlite3.Connection, *, row_count: int) -> None:
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, content_hash, "
        "created_at, updated_at) VALUES ('trk-seed', 'inferred', 't', 'abc', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    for idx in range(row_count):
        stable_id = f"trk-{idx:05d}"
        conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, content_hash, "
            "created_at, updated_at) VALUES (?, 'inferred', 't', ?, "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (stable_id, f"hash-{idx:05d}"),
        )
        conn.execute(
            "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
            "modified_at, updated_at, origin_device_id) "
            "VALUES (?, 'bpm', '128', 'rekordbox', ?, ?, NULL)",
            (stable_id, _T_STAMP, _T_STAMP),
        )
    for seq in range(row_count + 1, row_count + 70_001):
        table_name = "track_fields" if seq % 3 == 0 else "tracks"
        row_pk = sync_stamp.encode_row_pk((f"noise-{seq}", "field"))
        conn.execute(
            "INSERT INTO local_changelog(table_name, row_pk, updated_at, "
            "origin_device_id, received_at) VALUES (?, ?, ?, ?, ?)",
            (table_name, row_pk, _T_STAMP, "dev-a", _T_STAMP),
        )
    for idx in range(row_count):
        stable_id = f"trk-{idx:05d}"
        row_pk = sync_stamp.encode_row_pk((stable_id, "bpm"))
        conn.execute(
            "INSERT INTO local_changelog(table_name, row_pk, updated_at, "
            "origin_device_id, received_at) VALUES (?, ?, ?, ?, ?)",
            ("track_fields", row_pk, _T_STAMP, "dev-a", _T_STAMP),
        )
    conn.commit()


def test_second_open_rw_is_constant_time_on_large_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] 30k stamped rows reopen [then] second open is fast and O(1) queries, [else stop]."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    state_schema.apply_migrations(conn)
    _seed_large_backfill_fixture(conn, row_count=30_000)
    conn.close()

    state_db.open_rw(path).close()

    backfill_queries: list[str] = []
    real_connect = sqlite3.connect

    def _tracing_connect(database: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real_connect(database, *args, **kwargs)

        def _trace(sql: str) -> None:
            normalized = sql.strip().upper()
            if normalized.startswith("PRAGMA"):
                return
            if MARKER_TABLE.upper() in normalized:
                backfill_queries.append(sql)
                return
            if "TRACK_FIELDS" in normalized and normalized.split(None, 1)[0] in {
                "SELECT",
                "UPDATE",
                "INSERT",
                "DELETE",
            }:
                backfill_queries.append(sql)

        conn.set_trace_callback(_trace)
        return conn

    monkeypatch.setattr(sqlite3, "connect", _tracing_connect)
    started = time.monotonic()
    state_db.open_rw(path).close()
    elapsed = time.monotonic() - started

    marker = real_connect(str(path)).execute(
        f"SELECT 1 FROM {MARKER_TABLE} WHERE marker = ?",
        (TRACK_FIELDS_STAMP_BACKFILL_MARKER,),
    ).fetchone()
    assert marker is not None
    # Three one-shot markers are consulted on every open (v15 track_fields
    # stamp backfill, v17 hub changelog stamp repair, and the AGENTS.md
    # regeneration cache added for issue #4015), each as one sqlite_master
    # existence probe plus one PK lookup. Constant in the number of ladder
    # steps plus cache checks, never in the number of rows.
    assert len(backfill_queries) <= 6
    assert elapsed < 1.0
