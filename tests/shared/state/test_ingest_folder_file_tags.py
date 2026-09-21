"""Folder import persists browser metadata for tracks without rekordbox."""
from __future__ import annotations

import json
import struct
import wave
from pathlib import Path

import pytest

from apps.shared import audio_files
from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import folder
from apps.shared.state.writer import StateWriter


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<h", 0))


def test_folder_import_writes_genre_and_comment_file_tags(
    state_conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unmapped browser rows need tag metadata without a rekordbox join."""
    audio_path = tmp_path / "local.wav"
    _write_wav(audio_path)
    entry = audio_files.AudioFile(audio_path, audio_path.stat().st_size, 1.0, ".wav")
    monkeypatch.setattr(folder, "collect_audio", lambda _roots: ([entry], [], 0))
    monkeypatch.setattr(
        audio_files,
        "read_metadata",
        lambda _path: audio_files.AudioMetadata(
            title="Local", genre="House", comment="Imported from tags"
        ),
    )
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="folder-test")
    try:
        folder.ingest_folder(writer, [tmp_path], dry_run=False)
    finally:
        writer.close()

    rows = state_conn.execute(
        "SELECT field_name, value_json, source FROM track_fields ORDER BY field_name"
    ).fetchall()
    assert [(row[0], json.loads(row[1]), row[2]) for row in rows] == [
        ("comments", "Imported from tags", "inferred"),
        ("genre", "House", "inferred"),
    ]
