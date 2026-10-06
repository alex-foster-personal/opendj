"""LIBM-170: a folder ingest never imports a stem separation output as a track.

- [if] a walked root holds a vocals stem next to its song [then] only the song becomes a row and the stem is counted, [else stop].
- [if] a released track is titled Instrumental [then] it is still imported, [else stop].
"""

from __future__ import annotations

import sqlite3
import struct
import wave
from pathlib import Path

import pytest

from apps.shared import audio_files
from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import folder
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("LIBM-170")


def _write_wav(path: Path, sample: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<hhhh", sample, sample, sample, sample))
    return path


def _ingest(
    state_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, root: Path, paths: list[Path]
) -> folder.FolderIngestReport:
    entries = [audio_files.AudioFile(p, p.stat().st_size, 1.0, ".wav") for p in paths]
    monkeypatch.setattr(folder, "collect_audio", lambda _roots: (entries, [], 0))
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="folder-test")
    return folder.ingest_folder(writer, [root], dry_run=False, allow_mass_missing=True)


def _live_paths(state_conn: sqlite3.Connection) -> set[str]:
    rows = state_conn.execute("SELECT file_path FROM tracks WHERE deleted_at IS NULL")
    return {str(row[0]) for row in rows}


def test_vocals_stem_is_skipped_and_song_is_imported(
    state_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a root holds a song and its vocals/ stem [then] only the song is a row and one stem is counted, [else stop]."""
    song = _write_wav(tmp_path / "pack" / "033 - Artist - Song.wav", 3)
    stem = _write_wav(tmp_path / "pack" / "vocals" / "033 - Artist - Song - vocals.wav", 5)
    separated = _write_wav(tmp_path / "pack" / "stems" / "033-drums.wav", 9)

    report = _ingest(state_conn, monkeypatch, tmp_path, [song, stem, separated])

    assert _live_paths(state_conn) == {str(song)}
    assert report.files_skipped_stem_output == 2
    assert report.tracks_inserted == 1


def test_released_instrumental_is_still_imported(
    state_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a released track is named Song - Instrumental outside any stem dir [then] it is imported, [else stop]."""
    instrumental = _write_wav(tmp_path / "Album" / "04 Song - Instrumental.wav", 4)

    report = _ingest(state_conn, monkeypatch, tmp_path, [instrumental])

    assert _live_paths(state_conn) == {str(instrumental)}
    assert report.files_skipped_stem_output == 0
