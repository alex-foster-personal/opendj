"""Folder import persists browser metadata for tracks without rekordbox."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared import audio_files
from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import folder
from apps.shared.state.writer import StateWriter


def test_folder_import_writes_genre_and_comment_file_tags(
    state_conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unmapped browser rows need tag metadata without a rekordbox join."""
    audio_path = tmp_path / "local.mp3"
    audio_path.write_bytes(b"audio")
    entry = audio_files.AudioFile(audio_path, 5, 1.0, ".mp3")
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
