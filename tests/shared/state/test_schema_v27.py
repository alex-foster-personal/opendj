"""Migration v27: path_availability.mtime_ns (issue #7584).

[if] a v26 db is opened [then] it gains mtime_ns and keeps existing rows, [else stop].
[if] open_rw runs twice [then] the ALTER is idempotent, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.engine_core.store import schema as store_schema
from apps.shared.state import db as state_db
from apps.shared.state import migrations_v27
from apps.shared.state import schema as state_schema

pytestmark = pytest.mark.requirement("QLTSCAN-03")


def _ladder_to(path: Path, version: int) -> None:
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    for step_idx in range(version):
        for stmt in state_schema.MIGRATIONS[step_idx]:
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_meta(version, applied_at) VALUES (?, 'then')", (step_idx + 1,))
    conn.commit()
    conn.close()


def _path_avail_columns(conn: sqlite3.Connection) -> list[str]:
    return [str(r[1]) for r in conn.execute("PRAGMA table_info(path_availability)")]


def test_v26_db_gains_mtime_ns_on_open(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _ladder_to(path, 26)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO path_availability(resolver_namespace, logical_path, materialised_size, checked_at) "
        "VALUES ('ns', '/a.mp3', 10, '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    opened = state_db.open_rw(path)
    try:
        assert state_schema.apply_migrations(opened) == 27
        assert "mtime_ns" in _path_avail_columns(opened)
        row = opened.execute(
            "SELECT materialised_size, mtime_ns FROM path_availability WHERE logical_path = '/a.mp3'"
        ).fetchone()
        assert row == (10, None)
    finally:
        opened.close()


def test_second_open_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    state_db.open_rw(path, apply_schema=True).close()
    again = state_db.open_rw(path)
    try:
        assert state_schema.apply_migrations(again) == 27
        assert "mtime_ns" in _path_avail_columns(again)
    finally:
        again.close()


def test_fresh_databases_expose_mtime_ns(tmp_path: Path) -> None:
    legacy = state_db.open_rw(tmp_path / "legacy.db", apply_schema=True)
    try:
        assert "mtime_ns" in _path_avail_columns(legacy)
    finally:
        legacy.close()

    consolidated = sqlite3.connect(":memory:")
    store_schema.apply_migrations(consolidated)
    assert "mtime_ns" in _path_avail_columns(consolidated)
    consolidated.close()


def test_v27_step_matches_the_ladder_module() -> None:
    assert state_schema.MIGRATIONS[26] is migrations_v27._V27
