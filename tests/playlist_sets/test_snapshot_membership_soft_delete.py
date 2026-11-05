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
from tests.shared.playlist_sets.conftest import (
    seed_playlist_and_tracks,
    seed_playlist_membership,
)

NOW = "2026-09-01T00:00:00+00:00"


def test_a_soft_deleted_membership_is_excluded_from_the_snapshot(
    tmp_path: Path,
) -> None:
    conn = state_db.open_rw(tmp_path / "state.db")
    try:
        seed_playlist_and_tracks(conn, "pl1", ["trk-live", "trk-gone"], now=NOW)
        seed_playlist_membership(conn, "pl1", "trk-live", 0)
        seed_playlist_membership(conn, "pl1", "trk-gone", 1, deleted_at=NOW)
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
        conn.execute("CREATE TABLE playlist_memberships(playlist_id TEXT, stable_id TEXT, position INTEGER)")
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
