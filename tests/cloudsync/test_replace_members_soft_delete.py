"""``_replace_members`` must not resurrect a membership for a soft-deleted track.

Same class of defect as issue #2410 item 4 (apps/lyrics/batch.py,
apps/shared/playlist_sets/store.py): an unfiltered read of ``tracks``
treated a tombstoned row as present. Here the read is an existence probe
inside a whole-playlist membership replace (ADR 04 c5); a soft-deleted
track's parent row still matches ``SELECT 1 FROM tracks WHERE stable_id=?``,
so an incoming membership bundle naming it would have been inserted anyway.

[if] an incoming member names a stable_id whose local `tracks` row is
     soft-deleted [then] it is skipped like a genuinely absent track, never
     inserted [else broken].
[if] a live track's membership is present in the same bundle [then] it is
     still inserted [else broken] (control: the fix must not over-refuse).
"""
from __future__ import annotations

from pathlib import Path

from apps.sync_hub.engine_apply import _replace_members
from apps.sync_hub.protocol import MEMBERSHIP_TABLE, table_columns
from apps.shared.state import db as state_db

NOW = "2026-09-01T00:00:00+00:00"
_DEV = "dev-a"


def _seed_track(conn, stable_id: str, *, deleted: bool) -> None:
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, "
        "created_at, updated_at, deleted_at) VALUES (?, 'inferred', ?, "
        "?, ?, ?)",
        (stable_id, stable_id, NOW, NOW, NOW if deleted else None),
    )


def _seed_playlist(conn, playlist_id: str) -> None:
    conn.execute(
        "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, "
        "created_at, updated_at) VALUES (?, ?, 'rekordbox', ?, ?, ?)",
        (playlist_id, f"pl-{playlist_id}", playlist_id, NOW, NOW),
    )


def _member(columns: tuple[str, ...], playlist_id: str, stable_id: str, position: int) -> dict:
    row = {c: None for c in columns}
    row.update(
        playlist_id=playlist_id, stable_id=stable_id, position=position,
        updated_at=NOW, origin_device_id=_DEV,
    )
    return row


def test_a_soft_deleted_tracks_row_is_skipped_not_inserted(tmp_path: Path) -> None:
    conn = state_db.open_rw(tmp_path / "state.db")
    try:
        _seed_track(conn, "trk-live", deleted=False)
        _seed_track(conn, "trk-gone", deleted=True)
        _seed_playlist(conn, "pl1")
        columns = table_columns(conn, MEMBERSHIP_TABLE)
        members = [
            _member(columns, "pl1", "trk-live", 0),
            _member(columns, "pl1", "trk-gone", 1),
        ]
        _replace_members(conn, "pl1", members)
        rows = conn.execute(
            "SELECT stable_id FROM playlist_memberships WHERE playlist_id=?",
            ("pl1",),
        ).fetchall()
        assert [r[0] for r in rows] == ["trk-live"]
    finally:
        conn.close()
