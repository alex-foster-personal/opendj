"""LIBM-140: a track the user removed stays removed until the user restores it.

[if] rekordbox still lists a track the user removed [then] a re-ingest leaves it
removed and counts it as skipped: deleted by user, [else stop].

[if] the user restores a removed track [then] the next ingest keeps it live
(the fix must not make a track un-restorable), [else stop].

[if] a removed recording reappears under a new path with the same audio bytes
[then] it is the same recording and stays removed, [else stop].

[if] a file the library has never held is ingested [then] it still arrives,
[else stop].
"""

from __future__ import annotations

import shutil
import sqlite3
import struct
import wave
from pathlib import Path

import pytest

from apps.shared import audio_files
from apps.shared.state import db as state_db
from apps.shared.state import deleted_tracks
from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import folder
from apps.shared.state.ingest import rekordbox as rb_ingest
from apps.shared.state.writer import StateWriter
from apps.shared.state.writer_tracks import TrackRemovedError

pytestmark = pytest.mark.requirement("LIBM-140")


def _write_wav(path: Path, sample: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<hhhh", sample, sample, sample, sample))


class _RekordboxSource:
    """What rekordbox lists: the rows and playlists ``ingest_rb`` reads.

    Stands in for the vendor's database file only (the pattern of
    ``test_ingest_rekordbox_streaming``); every byte of app behavior under
    test, the ingest loop and the writer, is the real one. The committed
    rekordbox fixture lives on an external fixture host, so a test built on
    it cannot run on a machine without that drive.
    """

    def __init__(self, root: Path, names: tuple[str, ...]) -> None:
        self.rows = [_rb_row(root, index, name) for index, name in enumerate(names)]

    def close(self) -> None:
        return None


def _rb_row(root: Path, index: int, name: str) -> dict[str, object]:
    path = root / f"{name}.wav"
    _write_wav(path, index + 1)
    return {
        "id": str(100 + index),
        "title": name,
        "artist": "Artist",
        "album": "Album",
        "folder_path": str(path),
        "is_streaming": False,
        "isrc": None,
        "bpm": 120.0 + index,
        "rating": None,
        "duration_ms": 1000,
        "file_size": path.stat().st_size,
        "key_name": None,
        "updated_at": None,
    }


def _ingest(
    writer: StateWriter,
    monkeypatch: pytest.MonkeyPatch,
    source: _RekordboxSource,
    *,
    limit: int | None = None,
) -> rb_ingest.IngestReport:
    import pyrekordbox

    monkeypatch.setattr(pyrekordbox, "Rekordbox6Database", lambda *a, **k: source, raising=False)
    monkeypatch.setattr(rb_ingest, "_rb_rows", lambda _rb: iter(source.rows))
    monkeypatch.setattr(
        rb_ingest,
        "_rb_playlists",
        lambda _rb: iter([("1", "Set", [str(row["id"]) for row in source.rows])]),
    )
    return rb_ingest.ingest_rb(writer, Path("rekordbox.db"), dry_run=False, limit=limit)


@pytest.fixture
def source(tmp_path: Path) -> _RekordboxSource:
    return _RekordboxSource(tmp_path / "music", ("alpha", "bravo", "charlie", "delta"))


@pytest.fixture
def writer_and_conn(state_db_path: Path):
    conn = state_db.open_rw(state_db_path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="ingest-rb")
    try:
        yield writer, conn
    finally:
        writer.close()
        conn.close()


# ----- helpers -----------------------------------------------------------


def _row(conn: sqlite3.Connection, stable_id: str) -> tuple[object, ...]:
    row = conn.execute(
        "SELECT title, deleted_at, restored_at, updated_at FROM tracks WHERE stable_id = ?",
        (stable_id,),
    ).fetchone()
    assert row is not None, f"track {stable_id} vanished"
    return tuple(row)


def _a_track_in_a_playlist(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT m.stable_id FROM playlist_memberships m "
        "JOIN tracks t ON t.stable_id = m.stable_id "
        "WHERE m.deleted_at IS NULL AND t.deleted_at IS NULL "
        "ORDER BY m.stable_id LIMIT 1"
    ).fetchone()
    assert row is not None, "the ingest wrote no playlist member to remove"
    return str(row[0])


def _live_memberships(conn: sqlite3.Connection, stable_id: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM playlist_memberships WHERE stable_id = ? AND deleted_at IS NULL",
            (stable_id,),
        ).fetchone()[0]
    )


def _ingest_files(
    writer: StateWriter, monkeypatch: pytest.MonkeyPatch, root: Path, paths: list[Path]
) -> folder.FolderIngestReport:
    entries = [
        audio_files.AudioFile(path, path.stat().st_size, 1.0 + index, ".wav") for index, path in enumerate(paths)
    ]
    monkeypatch.setattr(folder, "collect_audio", lambda _roots: (entries, [], 0))
    return folder.ingest_folder(writer, [root], dry_run=False, allow_mass_missing=True)


# ----- rekordbox re-ingest ------------------------------------------------


def test_rekordbox_reingest_leaves_a_removed_track_removed(
    source: _RekordboxSource, writer_and_conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, conn = writer_and_conn
    first = _ingest(writer, monkeypatch, source)
    assert first.tracks_inserted > 0
    assert first.tracks_skipped_deleted == 0
    stable_id = _a_track_in_a_playlist(conn)
    assert _live_memberships(conn, stable_id) > 0
    removed = writer.remove_from_library(stable_id)
    before = _row(conn, stable_id)
    events_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    again = _ingest(writer, monkeypatch, source)

    assert _row(conn, stable_id) == before, "a rekordbox re-ingest rewrote a track the user removed"
    assert before[1] == removed.deleted_at
    assert again.tracks_skipped_deleted == 1
    assert again.tracks_inserted == 0
    assert _live_memberships(conn, stable_id) == 0, "the re-ingest put a removed track back into a playlist"
    undeletes = conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'track.undelete'").fetchone()[0]
    assert undeletes == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] >= events_before


def test_rekordbox_reingest_summary_names_the_skipped_deletes(
    source: _RekordboxSource,
    writer_and_conn,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer, conn = writer_and_conn
    _ingest(writer, monkeypatch, source)
    writer.remove_from_library(_a_track_in_a_playlist(conn))
    report = _ingest(writer, monkeypatch, source)

    rb_ingest._print_summary(report)

    assert report.to_dict()["tracks_skipped_deleted"] == 1
    assert "skipped: deleted by user: 1" in capsys.readouterr().out


def test_restored_track_stays_live_across_a_reingest(
    source: _RekordboxSource, writer_and_conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overshoot control: a restore must be honored, not skipped forever."""
    writer, conn = writer_and_conn
    _ingest(writer, monkeypatch, source)
    stable_id = _a_track_in_a_playlist(conn)
    writer.remove_from_library(stable_id)
    _ingest(writer, monkeypatch, source)

    restored = writer.undelete_track(stable_id)
    again = _ingest(writer, monkeypatch, source)

    assert restored.deleted_at is None
    _title, deleted_at, restored_at, _updated_at = _row(conn, stable_id)
    assert deleted_at is None, "an explicit restore was undone by the next ingest"
    assert restored_at is not None, "a restore must record when the user restored"
    assert again.tracks_skipped_deleted == 0
    assert _live_memberships(conn, stable_id) > 0


def test_a_track_new_to_the_library_still_ingests(
    source: _RekordboxSource, writer_and_conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: skipping removed tracks must not skip tracks never held."""
    writer, conn = writer_and_conn
    partial = _ingest(writer, monkeypatch, source, limit=2)
    assert partial.tracks_inserted > 0
    removed_id = str(conn.execute("SELECT stable_id FROM tracks ORDER BY stable_id LIMIT 1").fetchone()[0])
    writer.remove_from_library(removed_id)

    full = _ingest(writer, monkeypatch, source)

    assert full.tracks_inserted > 0, "a track rekordbox added never arrived"
    assert full.tracks_skipped_deleted == 1
    live = conn.execute("SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL").fetchone()[0]
    assert live == partial.tracks_inserted - 1 + full.tracks_inserted
    assert _row(conn, removed_id)[1] is not None


# ----- writer chokepoint --------------------------------------------------


@pytest.mark.parametrize("stored_reason", ["user", None])
def test_upsert_refuses_to_rewrite_a_removed_track(writer_and_conn, stored_reason: str | None) -> None:
    """``None`` is a tombstone written before the reason column existed: it is
    the user's until proven otherwise, never a missing file."""
    writer, conn = writer_and_conn
    seed = {
        "stable_id": "trk-1",
        "stable_id_tier": "inferred",
        "title": "Before",
        "artists": ["A"],
        "album": None,
        "isrc": None,
        "duration_ms": 1000,
        "file_path": None,
    }
    writer.upsert_track(**seed)
    writer.remove_from_library("trk-1")
    conn.execute("UPDATE tracks SET deleted_reason = ? WHERE stable_id = 'trk-1'", (stored_reason,))
    before = _row(conn, "trk-1")

    with pytest.raises(TrackRemovedError):
        writer.upsert_track(**{**seed, "title": "After"})
    with pytest.raises(TrackRemovedError):
        writer.upsert_track(**seed)

    assert _row(conn, "trk-1") == before


# ----- content identity: the same recording under a new path --------------


def test_same_audio_under_a_new_path_stays_removed(
    state_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "music" / "song.wav"
    _write_wav(original, 7)
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="folder-test")
    try:
        first = _ingest_files(writer, monkeypatch, tmp_path, [original])
        assert first.tracks_inserted == 1
        stable_id = str(state_conn.execute("SELECT stable_id FROM tracks").fetchone()[0])
        writer.remove_from_library(stable_id)

        moved = tmp_path / "music" / "renamed" / "song copy.wav"
        moved.parent.mkdir(parents=True)
        shutil.copy2(original, moved)
        different = tmp_path / "music" / "other.wav"
        _write_wav(different, 9)
        second = _ingest_files(writer, monkeypatch, tmp_path, [moved, different])
    finally:
        writer.close()

    assert second.tracks_skipped_deleted == 1, "the removed recording came back under its new path"
    assert second.tracks_inserted == 1, "a different recording must still arrive"
    live_paths = [str(row[0]) for row in state_conn.execute("SELECT file_path FROM tracks WHERE deleted_at IS NULL")]
    assert live_paths == [str(different)]


def test_same_audio_ingests_again_once_the_recording_is_restored(
    state_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overshoot control: a restored recording no longer blocks its own audio."""
    original = tmp_path / "music" / "song.wav"
    _write_wav(original, 7)
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="folder-test")
    try:
        _ingest_files(writer, monkeypatch, tmp_path, [original])
        stable_id = str(state_conn.execute("SELECT stable_id FROM tracks").fetchone()[0])
        writer.remove_from_library(stable_id)
        writer.undelete_track(stable_id)
        moved = tmp_path / "music" / "renamed" / "song copy.wav"
        moved.parent.mkdir(parents=True)
        shutil.copy2(original, moved)

        again = _ingest_files(writer, monkeypatch, tmp_path, [original, moved])
    finally:
        writer.close()

    assert again.tracks_skipped_deleted == 0
    assert again.tracks_inserted == 1, "a copy of a live recording was refused"


# ----- seeing and reversing deletes ----------------------------------------


def test_deleted_listing_names_removed_tracks_only(writer_and_conn) -> None:
    writer, conn = writer_and_conn
    for stable_id in ("keep-1", "gone-1"):
        writer.upsert_track(
            stable_id=stable_id,
            stable_id_tier="inferred",
            title=stable_id,
            artists=["A"],
            album=None,
            isrc=None,
            duration_ms=1000,
            file_path=None,
        )
    assert deleted_tracks.list_deleted(conn) == []
    removed = writer.remove_from_library("gone-1")

    listed = deleted_tracks.list_deleted(conn)

    assert [(item.stable_id, item.deleted_at) for item in listed] == [("gone-1", removed.deleted_at)]
    writer.undelete_track("gone-1")
    assert deleted_tracks.list_deleted(conn) == []
