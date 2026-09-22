"""Smartlist-test fixtures.

Self-contained Phase 5 DDL copy so tests don't depend on Phase 5 conftest.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.shared.pairings import PairingsRepo, ensure_phase08_tables
from apps.smartlists.repo import SmartlistsRepo

_PHASE5_TEST_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tracks (
        stable_id       TEXT PRIMARY KEY,
        stable_id_tier  TEXT NOT NULL,
        title           TEXT,
        artists_json    TEXT,
        album           TEXT,
        isrc            TEXT,
        duration_ms     INTEGER,
        file_path       TEXT,
        content_hash    TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        deleted_at      TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS track_fields (
        stable_id    TEXT NOT NULL,
        field_name   TEXT NOT NULL,
        value_json   TEXT NOT NULL,
        source       TEXT NOT NULL,
        confidence   REAL,
        modified_at  TEXT NOT NULL,
        PRIMARY KEY (stable_id, field_name)
    )
    """,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@pytest.fixture
def state_conn(tmp_path: Path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    for stmt in _PHASE5_TEST_DDL:
        conn.execute(stmt)
    ensure_phase08_tables(conn)
    try:
        yield conn
    finally:
        conn.close()


def _insert_track(conn, stable_id, *, title="untitled", created_at=None):
    ts = created_at or _now_iso()
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, "
        "updated_at) VALUES (?, 'isrc', ?, ?, ?)",
        (stable_id, title, ts, ts),
    )


def _insert_field(conn, stable_id, field, value, *, source="rekordbox"):
    conn.execute(
        "INSERT OR REPLACE INTO track_fields (stable_id, field_name, "
        "value_json, source, modified_at) VALUES (?, ?, ?, ?, ?)",
        (stable_id, field, json.dumps(value), source, _now_iso()),
    )


@pytest.fixture
def fixture_library(state_conn):
    class _Library:
        def __init__(self, conn):
            self.conn = conn

        def add_track(self, stable_id, *, title="t", created_at=None):
            _insert_track(self.conn, stable_id, title=title,
                          created_at=created_at)

        def add_field(self, stable_id, field, value, *, source="rekordbox"):
            _insert_field(self.conn, stable_id, field, value, source=source)

        def add_many(self, rows):
            for row in rows:
                self.add_track(
                    row["stable_id"],
                    title=row.get("title", "t"),
                    created_at=row.get("created_at"),
                )
                for key, val in row.get("fields", {}).items():
                    self.add_field(row["stable_id"], key, val)

    return _Library(state_conn)


@pytest.fixture
def smartlists_repo(state_conn) -> SmartlistsRepo:
    return SmartlistsRepo(state_conn, ensure_schema=False)


@pytest.fixture
def pairings_repo_slm(state_conn) -> PairingsRepo:
    return PairingsRepo(state_conn, ensure_schema=False)
