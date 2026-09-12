"""Shared fixtures for playlist-set tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.play_orders.schema import apply_play_order_migrations
from apps.shared.playlist_sets.schema import apply_playlist_set_migrations


@pytest.fixture
def ps_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    apply_play_order_migrations(conn)
    apply_playlist_set_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def ps_conn_with_memberships(ps_conn: sqlite3.Connection) -> sqlite3.Connection:
    ps_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS playlist_memberships (
            playlist_id  TEXT NOT NULL,
            stable_id    TEXT NOT NULL,
            position     INTEGER NOT NULL,
            PRIMARY KEY (playlist_id, position)
        )
        """
    )
    return ps_conn
