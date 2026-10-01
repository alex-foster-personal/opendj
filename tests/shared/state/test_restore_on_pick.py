"""LIBM-141: a removed track the user picks to add again comes back.

[if] a by-hand add (``restore_removed``) meets a file under a removed id
[then] that row is restored, live, with ``restored_at`` stamped [else fail].
[if] it meets the removed recording under a new path [then] the file arrives
as a live row and the old tombstone is left as it is [else fail].
[if] the caller does not pass ``restore_removed`` [then] the removed track
stays removed (LIBM-140 control) [else fail].
"""

from __future__ import annotations

import shutil
import sqlite3
import struct
import wave
from pathlib import Path

import pytest

from apps.shared import audio_files
from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import folder
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("LIBM-141")


def _write_wav(path: Path, sample: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<hhhh", sample, sample, sample, sample))


def _ingest(
    writer: StateWriter,
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    paths: list[Path],
    *,
    restore_removed: bool,
) -> folder.FolderIngestReport:
    entries = [audio_files.AudioFile(path, path.stat().st_size, 1.0, ".wav") for path in paths]
    monkeypatch.setattr(folder, "collect_audio", lambda _roots: (entries, [], 0))
    return folder.ingest_folder(
        writer, [root], dry_run=False, allow_mass_missing=True, restore_removed=restore_removed
    )


def _removed_song(
    state_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[StateWriter, Path, str]:
    song = tmp_path / "music" / "song.wav"
    _write_wav(song, 7)
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="folder-test")
    _ingest(writer, monkeypatch, tmp_path, [song], restore_removed=False)
    stable_id = str(state_conn.execute("SELECT stable_id FROM tracks").fetchone()[0])
    writer.remove_from_library(stable_id)
    return writer, song, stable_id


def _state(conn: sqlite3.Connection, stable_id: str) -> tuple[object, object]:
    row = conn.execute(
        "SELECT deleted_at, restored_at FROM tracks WHERE stable_id = ?", (stable_id,)
    ).fetchone()
    assert row is not None, f"track {stable_id} vanished"
    return row[0], row[1]


def test_picking_a_removed_file_again_restores_its_row(
    state_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, song, stable_id = _removed_song(state_conn, tmp_path, monkeypatch)
    try:
        report = _ingest(writer, monkeypatch, tmp_path, [song], restore_removed=True)
    finally:
        writer.close()

    deleted_at, restored_at = _state(state_conn, stable_id)
    assert deleted_at is None, "the file the user picked again stayed removed"
    assert restored_at is not None, "a restore must stamp restored_at so it outranks the tombstone"
    assert report.tracks_restored == 1
    assert report.tracks_skipped_deleted == 0


def test_picking_the_removed_recording_under_a_new_path_adds_it(
    state_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, song, stable_id = _removed_song(state_conn, tmp_path, monkeypatch)
    dropped = tmp_path / "_ingest" / "batch-1" / "song.wav"
    dropped.parent.mkdir(parents=True)
    shutil.copy2(song, dropped)
    try:
        report = _ingest(writer, monkeypatch, tmp_path, [dropped], restore_removed=True)
    finally:
        writer.close()

    live = [
        str(row[0])
        for row in state_conn.execute("SELECT file_path FROM tracks WHERE deleted_at IS NULL")
    ]
    assert live == [str(dropped)], "the dropped copy of a removed recording was not added"
    assert _state(state_conn, stable_id)[0] is not None, "the old row's tombstone was lifted"
    assert report.tracks_inserted == 1


def test_a_rescan_without_restore_removed_still_leaves_it_removed(
    state_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overshoot control: restoring is opt-in, never what a library rescan does."""
    writer, song, stable_id = _removed_song(state_conn, tmp_path, monkeypatch)
    try:
        report = _ingest(writer, monkeypatch, tmp_path, [song], restore_removed=False)
    finally:
        writer.close()

    assert _state(state_conn, stable_id)[0] is not None
    assert report.tracks_skipped_deleted == 1
    assert report.tracks_restored == 0
