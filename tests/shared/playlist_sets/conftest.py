"""Shared fixtures for playlist-set tests."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.shared.play_orders.schema import apply_play_order_migrations
from apps.shared.playlist_sets.schema import apply_playlist_set_migrations
from apps.shared.state.schema import apply_migrations


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
    """`playlist_memberships` is owned by the core state ladder
    (`apps.shared.state.migrations_v6_v8`), not by playlist_sets' own schema
    module -- a hand-rolled `CREATE TABLE` here had drifted from the real
    one, missing `updated_at`/`origin_device_id`/`deleted_at` (added v6-v8)
    and `item_id`/`order_key` (added v13). The real production schema was
    never missing these columns; only this fixture was stale, so every
    membership query this fixture ever fed a test was exercising a narrower
    shape than any real connection sees. Running the real migration ladder
    instead means this fixture can never drift from the schema it stands in
    for again.
    """
    apply_migrations(ps_conn)
    return ps_conn


def seed_playlist_and_tracks(
    conn: sqlite3.Connection, playlist_id: str, stable_ids: list[str]
) -> None:
    """Insert the minimal valid `playlists`/`tracks` parent rows a caller
    needs before inserting into `playlist_memberships`.

    `ps_conn_with_memberships` runs the REAL core-state migration ladder
    (see its docstring), so `playlist_memberships`'s `REFERENCES
    playlists(playlist_id)` / `REFERENCES tracks(stable_id)` constraints are
    now enforced (`PRAGMA foreign_keys = ON`, set by `ps_conn`). A test that
    inserts a membership row for a playlist_id/stable_id with no parent row
    is exercising a state the real schema forbids, so it needs real (if
    minimal) parent rows -- not a laxer fixture.
    """
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, "
        "created_at, updated_at) VALUES (?, ?, 'rekordbox', ?, ?, ?)",
        (playlist_id, playlist_id, playlist_id, now, now),
    )
    conn.executemany(
        "INSERT INTO tracks(stable_id, stable_id_tier, created_at, updated_at) "
        "VALUES (?, 'inferred', ?, ?)",
        [(stable_id, now, now) for stable_id in stable_ids],
    )
