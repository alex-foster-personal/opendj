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
        lambda _path: audio_files.AudioMetadata(title="Local", genre="House", comment="Imported from tags"),
    )
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="folder-test")
    try:
        folder.ingest_folder(writer, [tmp_path], dry_run=False)
    finally:
        writer.close()

    rows = state_conn.execute(
        "SELECT field_name, value_json, source FROM track_fields WHERE field_name != ? ORDER BY field_name",
        (folder.FILE_IDENTITY_FIELD,),
    ).fetchall()
    assert [(row[0], json.loads(row[1]), row[2]) for row in rows] == [
        ("comments", "Imported from tags", "inferred"),
        ("genre", "House", "inferred"),
    ]


def test_folder_import_reads_title_and_artist_without_mutagen(tmp_path: Path) -> None:
    """A real tagged mp3 still imports its title and artist. Reads use tinytag."""
    import shutil

    from apps.shared.state import db as state_db

    repo = Path(__file__).resolve().parents[3]
    fixture = repo / "tests" / "fixtures" / "phase7-dedup" / "src-v2.mp3"
    music = tmp_path / "music"
    music.mkdir()
    shutil.copy2(fixture, music / "src-v2.mp3")
    conn = state_db.open_rw(tmp_path / "state.db")
    writer = StateWriter(conn, bus=FakeEventBus(), actor="folder-test")
    try:
        folder.ingest_folder(writer, [music], dry_run=False)
    finally:
        writer.close()
    row = conn.execute("SELECT title, artists_json FROM tracks").fetchone()
    assert row[0] == "Source V2"
    assert json.loads(row[1]) == ["Fixture"]


# ----- LIBM-67b: a tag reload keeps the user's title, artist and album -----
def _tags(title: str, artist: str, album: str) -> audio_files.AudioMetadata:
    return audio_files.AudioMetadata(title=title, artist=artist, album=album, duration_s=1.0)


def _import_with_tags(state_conn, root: Path, monkeypatch: pytest.MonkeyPatch, tags: audio_files.AudioMetadata) -> None:
    """Run one real folder import of ``root/local.wav`` whose tag read answers ``tags``."""
    audio_path = root / "local.wav"
    if not audio_path.exists():
        _write_wav(audio_path)
    entry = audio_files.AudioFile(audio_path, audio_path.stat().st_size, audio_path.stat().st_mtime, ".wav")
    monkeypatch.setattr(folder, "collect_audio", lambda _roots: ([entry], [], 0))
    monkeypatch.setattr(audio_files, "read_metadata", lambda _path: tags)
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="folder-test")
    try:
        folder.ingest_folder(writer, [root], dry_run=False)
    finally:
        writer.close()


def _user_edit(
    state_conn, *, title: str | None = None, artists: list[str] | None = None, album: str | None = None
) -> None:
    """Correct the one row the way the webui PATCH does: an upsert of the tracks row by a user actor."""
    row = state_conn.execute(
        "SELECT stable_id, stable_id_tier, title, artists_json, album, isrc, duration_ms, file_path, "
        "content_hash, audio_hash FROM tracks"
    ).fetchone()
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="webui")
    try:
        writer.upsert_track(
            stable_id=row[0],
            stable_id_tier=row[1],
            title=title if title is not None else row[2],
            artists=artists if artists is not None else json.loads(row[3]),
            album=album if album is not None else row[4],
            isrc=row[5],
            duration_ms=row[6],
            file_path=row[7],
            content_hash=row[8],
            audio_hash=row[9],
        )
    finally:
        writer.close()


def _identity(state_conn) -> tuple[str, list[str], str]:
    title, artists, album = state_conn.execute("SELECT title, artists_json, album FROM tracks").fetchone()
    return title, json.loads(artists), album


@pytest.mark.requirement("LIBM-14651")
def test_a_reload_keeps_user_corrected_title_artist_and_album(
    state_conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the user corrected title, artist and album and the file is reloaded [then] all three corrections stay, [else stop]."""
    _import_with_tags(state_conn, tmp_path, monkeypatch, _tags("Tag Title", "Tag Artist", "Tag Album"))
    _user_edit(state_conn, title="My Title", artists=["My Artist"], album="My Album")

    _import_with_tags(state_conn, tmp_path, monkeypatch, _tags("Tag Title 2", "Tag Artist 2", "Tag Album 2"))

    assert _identity(state_conn) == ("My Title", ["My Artist"], "My Album"), (
        "if a reload clobbers a correction then broken"
    )


@pytest.mark.requirement("LIBM-14651")
def test_a_reload_refreshes_the_fields_the_user_did_not_correct(
    state_conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] only the title was corrected and the tags change [then] artist and album follow the file, title stays, [else stop]."""
    _import_with_tags(state_conn, tmp_path, monkeypatch, _tags("Tag Title", "Tag Artist", "Tag Album"))
    _user_edit(state_conn, title="My Title")

    _import_with_tags(state_conn, tmp_path, monkeypatch, _tags("Tag Title 2", "Tag Artist 2", "Tag Album 2"))

    assert _identity(state_conn) == ("My Title", ["Tag Artist 2"], "Tag Album 2"), (
        "if an uncorrected field is frozen then broken"
    )


@pytest.mark.requirement("LIBM-14651")
def test_a_reload_with_changed_tags_lands_them_and_records_the_new_baseline(
    state_conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] no field was corrected and the reload carries changed tags [then] the new tags land and become the baseline, [else stop]."""
    _import_with_tags(state_conn, tmp_path, monkeypatch, _tags("Tag Title", "Tag Artist", "Tag Album"))

    _import_with_tags(state_conn, tmp_path, monkeypatch, _tags("Tag Title 2", "Tag Artist 2", "Tag Album 2"))

    assert _identity(state_conn) == ("Tag Title 2", ["Tag Artist 2"], "Tag Album 2"), (
        "if a changed tag does not land then broken"
    )
    (sid,) = state_conn.execute("SELECT stable_id FROM tracks").fetchone()
    assert folder.read_file_identity(state_conn, sid) == folder.TrackIdentity(
        "Tag Title 2", ["Tag Artist 2"], "Tag Album 2"
    )
    history = state_conn.execute(
        "SELECT COUNT(*) FROM track_field_history WHERE field_name = ?", (folder.FILE_IDENTITY_FIELD,)
    ).fetchone()[0]
    _import_with_tags(state_conn, tmp_path, monkeypatch, _tags("Tag Title 2", "Tag Artist 2", "Tag Album 2"))
    assert (
        state_conn.execute(
            "SELECT COUNT(*) FROM track_field_history WHERE field_name = ?", (folder.FILE_IDENTITY_FIELD,)
        ).fetchone()[0]
        == history
    ), "if an unchanged reload rewrites the baseline then broken"


@pytest.mark.requirement("LIBM-14651")
def test_a_row_imported_before_the_baseline_keeps_a_non_fallback_value() -> None:
    """[if] a row has no recorded baseline and holds a non-fallback title [then] the reload keeps it, [else stop]."""
    stored = folder.TrackIdentity("Old Correction", [], None)
    from_file = folder.TrackIdentity("Tag Title", ["Tag Artist"], "Tag Album")

    merged = folder.merge_reloaded_identity(stored, from_file, None, fallback_title="local")

    assert merged == folder.TrackIdentity("Old Correction", ["Tag Artist"], "Tag Album")
