"""Migration v24: the never-read checked_at index on path_availability is dropped.

[if] a state.db reaches v24 [then] idx_path_availability_checked no longer exists, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.engine_core.store import schema as store_schema
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.webui.server.rb_vendor_pkg import path_index

pytestmark = pytest.mark.requirement("STATE-21")

INDEX = "idx_path_availability_checked"


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def _ladder_to(path: Path, version: int) -> None:
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    for step_idx in range(version):
        for stmt in state_schema.MIGRATIONS[step_idx]:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
            (step_idx + 1, "2026-10-01T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()


def test_v23_db_loses_the_index_on_open(tmp_path: Path) -> None:
    """[if] a v23 db holding the index opens [then] it is dropped and rows survive, [else stop]."""
    path = tmp_path / "state.db"
    _ladder_to(path, 23)
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO path_availability VALUES ('ns', '/a.mp3', 1, '2026-10-06T00:00:00+00:00')")
    conn.commit()
    assert INDEX in _indexes(conn)  # negative control: the old shape has it
    conn.close()

    state_db.open_rw(path).close()
    upgraded = sqlite3.connect(str(path))
    try:
        assert INDEX not in _indexes(upgraded)
        assert upgraded.execute("SELECT count(*) FROM path_availability").fetchone()[0] == 1
        assert upgraded.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] == 24
    finally:
        upgraded.close()


def test_fresh_ladders_never_keep_the_index() -> None:
    """[if] either ladder builds a fresh db [then] the index is absent, [else stop]."""
    legacy = sqlite3.connect(":memory:")
    state_schema.apply_migrations(legacy)
    store = sqlite3.connect(":memory:")
    store_schema.apply_migrations(store)
    try:
        assert INDEX not in _indexes(legacy)
        assert INDEX not in _indexes(store)
        assert "sqlite_autoindex_path_availability_1" in _indexes(legacy)
    finally:
        legacy.close()
        store.close()


def test_drop_is_safe_when_the_index_is_already_gone(tmp_path: Path) -> None:
    """[if] a v23 db never had the index [then] v24 still applies cleanly, [else stop]."""
    path = tmp_path / "state.db"
    _ladder_to(path, 23)
    conn = sqlite3.connect(str(path))
    conn.execute(f"DROP INDEX {INDEX}")
    conn.commit()
    conn.close()
    state_db.open_rw(path).close()
    check = sqlite3.connect(str(path))
    try:
        assert check.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] == 24
    finally:
        check.close()


def test_path_queries_use_the_primary_key(tmp_path: Path) -> None:
    """[if] the index lookup and upsert are planned [then] they use the primary key, [else stop]."""
    conn = state_db.open_rw(tmp_path / "state" / "state.db")
    try:
        path_index.upsert_rows(conn, "ns", [("/a.mp3", 1)])
        plan = " ".join(
            str(row[-1])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT logical_path, materialised_size, checked_at "
                "FROM path_availability WHERE resolver_namespace = ? AND logical_path IN (?, ?)",
                ("ns", "/a.mp3", "/b.mp3"),
            )
        )
        assert "sqlite_autoindex_path_availability_1" in plan
        assert path_index.bulk_lookup(conn, "ns", ["/a.mp3"])["/a.mp3"] is not None
    finally:
        conn.close()
