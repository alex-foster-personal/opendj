"""TAGIO-01: a folder import of REAL tagged files carries their tags into state.

No reader is stubbed: every file is encoded and tagged by ffmpeg, walked by
the real ``collect_audio``, probed and read by the real tag reader. This is
the path the packaged app runs, which read no tags at all while the reader
was the optional GPL mutagen.

Regression one-liners:
  - if an imported mp3/flac/m4a/aiff/ogg row loses its artist then broken
  - if files_without_tags is not 0 for a fully tagged folder then broken
  - if tag genre / bpm / key are not recorded with inferred provenance then broken
  - if a rescan overwrites an analysed bpm with the file's tag bpm then broken
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import folder
from apps.shared.state.writer import StateWriter
from tests.fixtures import tagged_audio as ta

pytestmark = [pytest.mark.requirement("TAGIO-01"), pytest.mark.requires_ffmpeg]

_IMPORTED_FORMATS = ("mp3-v24", "mp3-v23", "flac", "m4a", "aiff", "ogg", "wav")


@pytest.fixture()
def music_dir(tmp_path: Path) -> Path:
    root = tmp_path / "music"
    root.mkdir()
    for fmt in _IMPORTED_FORMATS:
        ta.make_tagged_audio(root, fmt)
    return root


def _import(state_conn, root: Path) -> folder.FolderIngestReport:
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="folder-test")
    try:
        return folder.ingest_folder(writer, [root], dry_run=False)
    finally:
        writer.close()


def _field(state_conn, file_name: str, field_name: str) -> tuple[object, str] | None:
    row = state_conn.execute(
        "SELECT f.value_json, f.source FROM track_fields f JOIN tracks t USING (stable_id) "
        "WHERE t.file_path LIKE ? AND f.field_name = ?",
        (f"%/{file_name}", field_name),
    ).fetchone()
    return (json.loads(row[0]), row[1]) if row else None


def test_every_imported_format_carries_its_artist_and_title(state_conn, music_dir: Path) -> None:
    """[if] a folder of tagged files is imported [then] rows carry artist + title, [else stop]."""
    report = _import(state_conn, music_dir)
    assert report.files_seen == len(_IMPORTED_FORMATS)
    assert report.files_without_tags == 0
    rows = state_conn.execute("SELECT title, artists_json, album, duration_ms FROM tracks").fetchall()
    assert len(rows) == len(_IMPORTED_FORMATS)
    for title, artists_json, album, duration_ms in rows:
        assert title == ta.STANDARD_TAGS["title"]
        assert json.loads(artists_json) == [ta.STANDARD_TAGS["artist"]]
        assert album == ta.STANDARD_TAGS["album"]
        assert duration_ms is not None and 900 <= duration_ms <= 1200


def test_genre_comment_bpm_and_key_are_recorded_as_inferred(state_conn, music_dir: Path) -> None:
    """[if] files carry genre/comment/bpm/key [then] they land as inferred, [else stop]."""
    report = _import(state_conn, music_dir)
    assert _field(state_conn, "flac.flac", "genre") == (ta.STANDARD_TAGS["genre"], "inferred")
    assert _field(state_conn, "flac.flac", "comments") == (ta.STANDARD_TAGS["comment"], "inferred")
    assert _field(state_conn, "mp3-v24.mp3", "bpm") == (float(ta.BPM), "inferred")
    assert _field(state_conn, "mp3-v24.mp3", "key") == (ta.KEY, "inferred")
    # Every format but wav (INFO has no BPM) carries a BPM; m4a has no key atom from ffmpeg.
    assert report.tracks_with_tag_bpm == len(_IMPORTED_FORMATS) - 1
    assert report.tracks_with_tag_key == len(_IMPORTED_FORMATS) - 2


def test_a_rescan_never_overwrites_an_existing_bpm_or_key(state_conn, music_dir: Path) -> None:
    """[if] a track has an analysed bpm/key [then] a rescan keeps it, [else stop]."""
    _import(state_conn, music_dir)
    (stable_id,) = state_conn.execute(
        "SELECT stable_id FROM tracks WHERE file_path LIKE '%/flac.flac'"
    ).fetchone()
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="analysis-test")
    try:
        writer.set_field(stable_id, "bpm", 127.98, source="open-dj-tool", modified_at="2026-10-01T09:00:00Z")
        writer.set_field(stable_id, "key", "3A", source="webui", modified_at="2026-10-01T09:00:00Z")
    finally:
        writer.close()

    report = _import(state_conn, music_dir)
    assert _field(state_conn, "flac.flac", "bpm") == (127.98, "open-dj-tool")
    assert _field(state_conn, "flac.flac", "key") == ("3A", "webui")
    assert report.tracks_with_tag_bpm == 0


def test_an_unparseable_file_is_still_imported_and_counted(state_conn, tmp_path: Path) -> None:
    """[if] a file has no readable tags [then] it imports, counted untagged, [else stop]."""
    root = tmp_path / "music"
    root.mkdir()
    ta.make_untagged_audio(root, "flac")
    report = _import(state_conn, root)
    assert report.tracks_inserted == 1
    assert report.files_without_tags == 1
    (title,) = state_conn.execute("SELECT title FROM tracks").fetchone()
    assert title == "untagged-flac"
