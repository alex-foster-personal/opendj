"""Shared fixtures for pairings + smartlists tests.

Creates a Phase 5 state DB with migrations applied and a ``PairingsRepo``
bound to a fresh connection per test.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.pairings import PairingsRepo, ensure_phase08_tables


@pytest.fixture
def state_conn(tmp_path: Path):
    """Yield a writable sqlite3 Connection with Phase 08 tables applied.

    Not shared with ``tests/shared/state/conftest.py`` because that
    conftest is Phase 5-owned and may be rewritten by the Phase 5
    executor. A self-contained fixture keeps Phase 08 tests stable
    regardless of Phase 5's current schema version.
    """
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    # Try to apply Phase 5 migrations if available (best-effort; Phase 5
    # ships the full ``tracks`` table needed by the integration / evaluator
    # tests). Phase 08 tables then come from ensure_phase08_tables.
    try:  # pragma: no cover - exercised indirectly via migration tests
        from apps.shared.state import schema as state_schema
        state_schema.apply_migrations(conn)
    except Exception:
        pass
    ensure_phase08_tables(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def pairings_repo(state_conn: sqlite3.Connection) -> PairingsRepo:
    # ensure_schema=False because state_conn already applied the DDL.
    return PairingsRepo(state_conn, ensure_schema=False)
