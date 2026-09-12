"""LIBM-52 StateWriter track remove/undelete lifecycle tests.

[if] remove_from_library tombstones a track and live memberships [then] undelete
restores only those memberships at original positions and never touches the
audio file, [else stop].
"""
from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter, compute_playlist_id
from apps.shared.state.writer_tracks import (
    TrackAlreadyRemovedError,
    TrackNotFoundError,
    TrackNotRemovedError,
    _TrackWriterMixin,
)

pytestmark = pytest.mark.requirement("LIBM-52")


@pytest.fixture
def writer(state_conn: sqlite3.Connection):
    bus = FakeEventBus()
    t0 = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)
    counter = {"n": 0}

    def clock() -> datetime:
        counter["n"] += 1
        return t0.replace(microsecond=counter["n"])

    w = StateWriter(state_conn, bus=bus, clock=clock, actor="unit-test")
    try:
        yield w
    finally:
        w.close()


def _seed_track(
    writer: StateWriter,
    stable_id: str,
    file_path: str | None = None,
) -> None:
    writer.upsert_track(
        stable_id=stable_id,
        stable_id_tier="inferred",
        title=f"title-{stable_id}",
        artists=["artist"],
        album=None,
        isrc=None,
        duration_ms=180_000,
        file_path=file_path,
    )


def test_remove_stamps_track_and_live_memberships_only(
    writer: StateWriter,
    state_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """[if] a track has live and already-tombstoned memberships [then] remove
    stamps only the live rows and leaves the audio file untouched, [else stop]."""
    audio = tmp_path / "track.flac"
    audio.write_bytes(b"audio-bytes")
    before = audio.stat()
    track_id = "t" * 40
    other_id = "o" * 40
    _seed_track(writer, track_id, str(audio))
    _seed_track(writer, other_id)
    pid_live = compute_playlist_id("webui", "live-pl")
    pid_old = compute_playlist_id("webui", "old-pl")
    writer.insert_playlist(
        playlist_id=pid_live, name="live", vendor="webui", vendor_pl_id="live-pl",
    )
    writer.insert_playlist(
        playlist_id=pid_old, name="old", vendor="webui", vendor_pl_id="old-pl",
    )
    writer.set_playlist_memberships(pid_live, [track_id])
    writer.set_playlist_memberships(pid_old, [track_id])
    old_deleted_at = "2026-09-10T00:00:00+00:00"
    state_conn.execute(
        "UPDATE playlist_memberships SET deleted_at = ? WHERE playlist_id = ?",
        (old_deleted_at, pid_old),
    )

    result = writer.remove_from_library(track_id)

    track_row = state_conn.execute(
        "SELECT deleted_at FROM tracks WHERE stable_id = ?", (track_id,),
    ).fetchone()
    assert track_row[0] == result.deleted_at
    live_membership = state_conn.execute(
        "SELECT deleted_at FROM playlist_memberships WHERE playlist_id = ?",
        (pid_live,),
    ).fetchone()
    assert live_membership[0] == result.deleted_at
    old_membership = state_conn.execute(
        "SELECT deleted_at FROM playlist_memberships WHERE playlist_id = ?",
        (pid_old,),
    ).fetchone()
    assert old_membership[0] == old_deleted_at
    assert audio.exists()
    assert audio.stat().st_mtime_ns == before.st_mtime_ns


def test_undelete_restores_only_matching_memberships(
    writer: StateWriter,
    state_conn: sqlite3.Connection,
) -> None:
    """[if] a removed track is undeleted [then] only memberships stamped by that
    remove are revived at their original positions, [else stop]."""
    track_id = "u" * 40
    other_id = "v" * 40
    _seed_track(writer, track_id)
    _seed_track(writer, other_id)
    pid_a = compute_playlist_id("webui", "a")
    pid_b = compute_playlist_id("webui", "b")
    writer.insert_playlist(
        playlist_id=pid_a, name="A", vendor="webui", vendor_pl_id="a",
    )
    writer.insert_playlist(
        playlist_id=pid_b, name="B", vendor="webui", vendor_pl_id="b",
    )
    writer.set_playlist_memberships(pid_a, [track_id])
    writer.set_playlist_memberships(pid_b, [other_id, track_id])

    removed = writer.remove_from_library(track_id)
    restored = writer.undelete_track(track_id)

    assert restored.deleted_at is None
    assert {(m.playlist_id, m.position) for m in restored.memberships} == {
        (pid_a, 0),
        (pid_b, 1),
    }
    assert state_conn.execute(
        "SELECT deleted_at FROM tracks WHERE stable_id = ?", (track_id,),
    ).fetchone()[0] is None
    for playlist_id, position in ((pid_a, 0), (pid_b, 1)):
        assert state_conn.execute(
            "SELECT deleted_at FROM playlist_memberships "
            "WHERE playlist_id = ? AND position = ?",
            (playlist_id, position),
        ).fetchone()[0] is None
    assert removed.deleted_at is not None


def test_remove_and_undelete_emit_events_and_changelog(
    writer: StateWriter,
    state_conn: sqlite3.Connection,
) -> None:
    """[if] remove then undelete run [then] events and local_changelog rows are
    stamped for tracks and memberships, [else stop]."""
    track_id = "e" * 40
    _seed_track(writer, track_id)
    pid = compute_playlist_id("webui", "evt")
    writer.insert_playlist(
        playlist_id=pid, name="evt", vendor="webui", vendor_pl_id="evt",
    )
    writer.set_playlist_memberships(pid, [track_id])
    writer.bus.events.clear()  # type: ignore[attr-defined]

    writer.remove_from_library(track_id)
    remove_kinds = [e.kind for e in writer.bus.events]  # type: ignore[attr-defined]
    assert remove_kinds[-1] == "track.delete"
    changelog_after_remove = state_conn.execute(
        "SELECT table_name FROM local_changelog"
    ).fetchall()
    assert ("tracks",) in changelog_after_remove
    assert ("playlist_memberships",) in changelog_after_remove

    writer.bus.events.clear()  # type: ignore[attr-defined]
    writer.undelete_track(track_id)
    undelete_kinds = [e.kind for e in writer.bus.events]  # type: ignore[attr-defined]
    assert undelete_kinds[-1] == "track.undelete"


def test_remove_and_undelete_error_cases(writer: StateWriter) -> None:
    """[if] remove/undelete preconditions fail [then] domain errors raise,
    [else stop]."""
    track_id = "x" * 40
    with pytest.raises(TrackNotFoundError):
        writer.remove_from_library(track_id)
    _seed_track(writer, track_id)
    writer.remove_from_library(track_id)
    with pytest.raises(TrackAlreadyRemovedError):
        writer.remove_from_library(track_id)
    writer.undelete_track(track_id)
    with pytest.raises(TrackNotRemovedError):
        writer.undelete_track(track_id)


def test_remove_from_library_source_has_no_file_deletes() -> None:
    """[if] remove_from_library is implemented [then] it never hard-deletes
    files or rows, [else stop]."""
    source = inspect.getsource(_TrackWriterMixin.remove_from_library)
    for forbidden in ("unlink", "os.remove", "DELETE FROM"):
        assert forbidden not in source
