"""``_snapshot_membership`` must exclude soft-deleted rows (issue #2410 item 4).

[if] a soft-deleted membership row is included in a snapshot [then] broken.
[if] a schema mismatch (a DB missing ``playlist_memberships.deleted_at``)
     is swallowed into an empty snapshot instead of raising [then] broken --
     a real absent table is the only case this function may treat as empty.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.playlist_sets.store import _snapshot_membership
from apps.shared.state import db as state_db

NOW = "2026-09-01T00:00:00+00:00"


def _seed_track(conn: sqlite3.Connection, stable_id: str) -> None:
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, "
        "created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?)",
        (stable_id, stable_id, NOW, NOW),
    )


def _seed_playlist(conn: sqlite3.Connection, playlist_id: str) -> None:
    conn.execute(
        "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, "
        "created_at, updated_at) VALUES (?, ?, 'rekordbox', ?, ?, ?)",
        (playlist_id, f"pl-{playlist_id}", playlist_id, NOW, NOW),
    )


def _seed_membership(
    conn: sqlite3.Connection, playlist_id: str, stable_id: str, position: int,
    *, deleted: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO playlist_memberships(playlist_id, stable_id, position, "
        "deleted_at) VALUES (?, ?, ?, ?)",
        (playlist_id, stable_id, position, NOW if deleted else None),
    )


def test_a_soft_deleted_membership_is_excluded_from_the_snapshot(
    tmp_path: Path,
) -> None:
    conn = state_db.open_rw(tmp_path / "state.db")
    try:
        _seed_track(conn, "trk-live")
        _seed_track(conn, "trk-gone")
        _seed_playlist(conn, "pl1")
        _seed_membership(conn, "pl1", "trk-live", 0)
        _seed_membership(conn, "pl1", "trk-gone", 1, deleted=True)
        conn.commit()
        assert _snapshot_membership(conn, "pl1") == [("trk-live", 0)]
    finally:
        conn.close()


def test_a_schema_mismatch_raises_rather_than_reads_empty(
    tmp_path: Path,
) -> None:
    """A missing column must fail loudly, unlike a genuinely absent table."""
    conn = sqlite3.connect(tmp_path / "bare.db")
    try:
        conn.execute(
            "CREATE TABLE playlist_memberships(playlist_id TEXT, "
            "stable_id TEXT, position INTEGER)"
        )
        with pytest.raises(sqlite3.OperationalError, match="deleted_at"):
            _snapshot_membership(conn, "pl1")
    finally:
        conn.close()


def test_a_genuinely_missing_table_still_reads_as_an_empty_snapshot(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(tmp_path / "empty.db")
    try:
        assert _snapshot_membership(conn, "pl1") == []
    finally:
        conn.close()
