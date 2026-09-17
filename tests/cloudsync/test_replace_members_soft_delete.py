"""``_replace_members`` writes exactly the winning bundle -- tombstoned
member tracks included -- and only skips a track it has never heard of.

Round 5 trunk-red fix (Mon 14 Sep 2026, issue trunk-red-sim-property-digest):
a prior fix (issue #2410 item 4, PR #2427) made this existence probe also
skip a member whose LOCAL track row is soft-deleted, treating it the same
as a genuinely-absent track. That looked like the right move under the
"every synced-table read filters deleted_at" guard
(``test_soft_delete_read_guard.py``), but ``_replace_members`` is not a
display-layer listing -- it is the sync engine writing ADR 04 c5's
whole-playlist bundle, and a soft-deleted track's row still EXISTS (no FK
violation). Filtering it out made the stored membership set depend on
THIS machine's own delivery-order history of the track's tombstone rather
than on the playlist's winning write, so two machines holding the
IDENTICAL winning (name, updated_at, origin) playlist version could still
diverge on ``playlist_memberships`` -- exactly the persistent digest
mismatch ``tests/cloudsync/test_sim_property.py::
test_fleet_converges_to_the_lww_oracle`` caught on trunk. The LWW oracle
(``tests/cloudsync/sim_oracle.py``) never filters membership by the
member track's ``deleted_at`` either, so this test now pins the oracle's
behavior: same class of defect as issue #2410 item 4 in spirit (an
unfiltered read must not silently resurface a tombstone to a USER), but
the opposite fix here (a WRITE must not silently drop a tombstoned member
the winning write actually named).

[if] an incoming member names a stable_id whose local `tracks` row is
     soft-deleted (present, tombstoned) [then] its membership is still
     inserted, exactly like a live track's [else broken].
[if] an incoming member names a stable_id with no local `tracks` row at
     all [then] it is skipped (FK safety: the row does not exist to
     reference) [else broken].
[if] the skip is logged as "not here yet" for a genuinely absent track
     [then] a soft-deleted-but-present track logs nothing at all, since it
     is no longer skipped [else broken].
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.sync_hub.engine_apply import _replace_members
from apps.sync_hub.protocol import MEMBERSHIP_TABLE, table_columns

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


def test_a_soft_deleted_tracks_row_is_still_inserted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The bundle is written verbatim: a live and a soft-deleted track's
    memberships both land, and neither logs a skip."""
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
        with caplog.at_level(logging.WARNING, logger="apps.sync_hub.engine"):
            _replace_members(conn, "pl1", members)
        rows = conn.execute(
            "SELECT stable_id FROM playlist_memberships WHERE playlist_id=? "
            "ORDER BY position",
            ("pl1",),
        ).fetchall()
        assert [r[0] for r in rows] == ["trk-live", "trk-gone"]
        messages = [r.getMessage() for r in caplog.records]
        assert not any("trk-gone" in m for m in messages), (
            f"a soft-deleted-but-present track must not be skipped or "
            f"logged as skipped: {messages}"
        )
    finally:
        conn.close()


def test_a_never_arrived_track_is_still_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Control: a track this machine has never heard of at all is still
    skipped (FK safety -- the fix above must not over-admit)."""
    conn = state_db.open_rw(tmp_path / "state.db")
    try:
        _seed_track(conn, "trk-live", deleted=False)
        _seed_playlist(conn, "pl1")
        columns = table_columns(conn, MEMBERSHIP_TABLE)
        members = [
            _member(columns, "pl1", "trk-live", 0),
            _member(columns, "pl1", "trk-unknown", 1),
        ]
        with caplog.at_level(logging.WARNING, logger="apps.sync_hub.engine"):
            _replace_members(conn, "pl1", members)
        rows = conn.execute(
            "SELECT stable_id FROM playlist_memberships WHERE playlist_id=?",
            ("pl1",),
        ).fetchall()
        assert [r[0] for r in rows] == ["trk-live"]
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "trk-unknown" in m and "not here yet" in m for m in messages
        )
    finally:
        conn.close()
