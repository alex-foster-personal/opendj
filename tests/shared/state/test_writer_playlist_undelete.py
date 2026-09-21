"""LIBMX-03 StateWriter playlist delete/undelete lifecycle tests.

[if] delete_playlist tombstones a playlist and live memberships [then] undelete_playlist restores only those memberships at original positions, [else stop].
"""
from __future__ import annotations

import inspect
import sqlite3
from datetime import UTC, datetime

import pytest

from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter, compute_playlist_id
from apps.shared.state.writer_playlists import (
    PlaylistNotDeletedError,
    PlaylistNotFoundError,
    _PlaylistWriterMixin,
)

pytestmark = pytest.mark.requirement("LIBMX-03")


@pytest.fixture
def writer(state_conn: sqlite3.Connection):
    bus = FakeEventBus()
    t0 = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    counter = {"n": 0}

    def clock() -> datetime:
        counter["n"] += 1
        return t0.replace(microsecond=counter["n"])

    w = StateWriter(state_conn, bus=bus, clock=clock, actor="unit-test")
    try:
        yield w
    finally:
        w.close()


def _seed_track(writer: StateWriter, stable_id: str) -> None:
    writer.upsert_track(
        stable_id=stable_id,
        stable_id_tier="inferred",
        title=f"title-{stable_id}",
        artists=["artist"],
        album=None,
        isrc=None,
        duration_ms=180_000,
        file_path=None,
    )


def test_delete_stamps_shared_tombstone(
    writer: StateWriter,
    state_conn: sqlite3.Connection,
) -> None:
    """[if] a playlist with live and pre-tombstoned memberships is deleted [then]
    live rows share the playlist tombstone timestamp, [else stop]."""
    track_a = "a" * 40
    track_b = "b" * 40
    track_old = "o" * 40
    _seed_track(writer, track_a)
    _seed_track(writer, track_b)
    _seed_track(writer, track_old)
    pid = compute_playlist_id("webui", "shared-tombstone")
    writer.insert_playlist(
        playlist_id=pid, name="shared", vendor="webui", vendor_pl_id="shared",
    )
    writer.set_playlist_memberships(pid, [track_a, track_b, track_old])
    old_deleted_at = "2026-09-10T00:00:00+00:00"
    state_conn.execute(
        "UPDATE playlist_memberships SET deleted_at = ? "
        "WHERE playlist_id = ? AND stable_id = ?",
        (old_deleted_at, pid, track_old),
    )

    assert writer.delete_playlist(pid) is True
    playlist_row = state_conn.execute(
        "SELECT deleted_at FROM playlists WHERE playlist_id = ?", (pid,),
    ).fetchone()
    assert playlist_row[0] is not None
    tombstone_ts = playlist_row[0]
    for stable_id in (track_a, track_b):
        row = state_conn.execute(
            "SELECT deleted_at FROM playlist_memberships "
            "WHERE playlist_id = ? AND stable_id = ?",
            (pid, stable_id),
        ).fetchone()
        assert row[0] == tombstone_ts
    old_row = state_conn.execute(
        "SELECT deleted_at FROM playlist_memberships "
        "WHERE playlist_id = ? AND stable_id = ?",
        (pid, track_old),
    ).fetchone()
    assert old_row[0] == old_deleted_at
    assert writer.bus.events[-1].kind == "playlist.delete"  # type: ignore[attr-defined]


def test_undelete_restores_only_matching_memberships(
    writer: StateWriter,
    state_conn: sqlite3.Connection,
) -> None:
    """[if] a deleted playlist is undeleted [then] only memberships from that
    delete revive with original ids and order keys, [else stop]."""
    track_a = "c" * 40
    track_b = "d" * 40
    track_old = "p" * 40
    _seed_track(writer, track_a)
    _seed_track(writer, track_b)
    _seed_track(writer, track_old)
    pid = compute_playlist_id("webui", "undelete-match")
    writer.insert_playlist(
        playlist_id=pid, name="match", vendor="webui", vendor_pl_id="match",
    )
    writer.set_playlist_memberships(pid, [track_a, track_b, track_old])
    old_deleted_at = "2026-09-10T00:00:00+00:00"
    state_conn.execute(
        "UPDATE playlist_memberships SET deleted_at = ? "
        "WHERE playlist_id = ? AND stable_id = ?",
        (old_deleted_at, pid, track_old),
    )
    before_live = state_conn.execute(
        "SELECT item_id, stable_id, position, order_key FROM playlist_memberships "
        "WHERE playlist_id = ? AND deleted_at IS NULL ORDER BY order_key",
        (pid,),
    ).fetchall()
    snapshot = {
        row[0]: (row[1], row[2], row[3])
        for row in state_conn.execute(
            "SELECT item_id, stable_id, position, order_key FROM playlist_memberships "
            "WHERE playlist_id = ? AND stable_id IN (?, ?)",
            (pid, track_a, track_b),
        ).fetchall()
    }

    writer.delete_playlist(pid)
    writer.bus.events.clear()  # type: ignore[attr-defined]
    writer.undelete_playlist(pid)

    after_live = state_conn.execute(
        "SELECT stable_id FROM playlist_memberships "
        "WHERE playlist_id = ? AND deleted_at IS NULL ORDER BY order_key",
        (pid,),
    ).fetchall()
    assert [row[0] for row in after_live] == [row[1] for row in before_live]
    for item_id, (stable_id, position, order_key) in snapshot.items():
        row = state_conn.execute(
            "SELECT stable_id, position, order_key, deleted_at "
            "FROM playlist_memberships WHERE playlist_id = ? AND item_id = ?",
            (pid, item_id),
        ).fetchone()
        assert row == (stable_id, position, order_key, None)
    old_row = state_conn.execute(
        "SELECT deleted_at FROM playlist_memberships "
        "WHERE playlist_id = ? AND stable_id = ?",
        (pid, track_old),
    ).fetchone()
    assert old_row[0] == old_deleted_at
    assert writer.bus.events[-1].kind == "playlist.undelete"  # type: ignore[attr-defined]


def test_undelete_error_cases(writer: StateWriter) -> None:
    """[if] undelete preconditions fail [then] domain errors raise, [else stop]."""
    pid = compute_playlist_id("webui", "missing")
    with pytest.raises(PlaylistNotFoundError):
        writer.undelete_playlist(pid)
    writer.insert_playlist(
        playlist_id=pid, name="live", vendor="webui", vendor_pl_id="live",
    )
    with pytest.raises(PlaylistNotDeletedError):
        writer.undelete_playlist(pid)
    writer.delete_playlist(pid)
    writer.undelete_playlist(pid)
    with pytest.raises(PlaylistNotDeletedError):
        writer.undelete_playlist(pid)


def test_delete_and_undelete_never_hard_delete(
    writer: StateWriter,
    state_conn: sqlite3.Connection,
) -> None:
    """[if] delete then undelete run [then] row counts are unchanged, [else stop]."""
    track_id = "n" * 40
    _seed_track(writer, track_id)
    pid = compute_playlist_id("webui", "no-hard-delete")
    writer.insert_playlist(
        playlist_id=pid, name="counts", vendor="webui", vendor_pl_id="counts",
    )
    writer.set_playlist_memberships(pid, [track_id])
    before_playlists = state_conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0]
    before_members = state_conn.execute(
        "SELECT COUNT(*) FROM playlist_memberships",
    ).fetchone()[0]

    writer.delete_playlist(pid)
    writer.undelete_playlist(pid)

    assert state_conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0] == before_playlists
    assert state_conn.execute(
        "SELECT COUNT(*) FROM playlist_memberships",
    ).fetchone()[0] == before_members


def test_delete_and_undelete_source_has_no_hard_deletes() -> None:
    """[if] delete_playlist and undelete_playlist are implemented [then] they
    never hard-delete rows, [else stop]."""
    for method_name in ("delete_playlist", "undelete_playlist"):
        source = inspect.getsource(getattr(_PlaylistWriterMixin, method_name))
        assert "DELETE FROM" not in source
