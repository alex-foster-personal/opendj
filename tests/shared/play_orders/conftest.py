"""Shared fixtures for play-order tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def po_conn(tmp_path: Path) -> sqlite3.Connection:
    """Fresh in-file SQLite connection with FKs on + autocommit.

    An in-file DB (not ``:memory:``) is used because some tests exercise
    the connection-close / reopen contract, which ``:memory:`` cannot
    survive.
    """
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def po_conn_with_events(po_conn: sqlite3.Connection) -> sqlite3.Connection:
    """Extends :func:`po_conn` with the Phase 5 ``events`` table.

    Simulates the state where Phase 5's event bus is online so we can
    assert events are persisted on mutation.
    """
    po_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT NOT NULL,
            kind         TEXT NOT NULL,
            stable_id    TEXT,
            payload_json TEXT,
            actor        TEXT
        )
        """
    )
    return po_conn


@pytest.fixture
def po_conn_with_memberships(
    po_conn: sqlite3.Connection,
) -> sqlite3.Connection:
    """Extends :func:`po_conn` with Phase 5's ``playlist_memberships``.

    Used by tests for the virtual ``"default"`` read-through order.
    """
    po_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS playlist_memberships (
            playlist_id  TEXT NOT NULL,
            stable_id    TEXT NOT NULL,
            position     INTEGER NOT NULL,
            PRIMARY KEY (playlist_id, position)
        )
        """
    )
    return po_conn
