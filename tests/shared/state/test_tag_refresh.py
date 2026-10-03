"""Rows imported while the packaged reader read nothing get their tags back.

Live on demon-llama, Thu 1 Oct 2026: all 100 rows of the testmac sample read
title = filename, no artist, no album, no duration, although every file is
tagged (AVIRA / Out Of Context), because they were imported by a build whose
reader was the absent GPL mutagen. A folder rescan never revisits a known row.

Regression one-liners:
  - if a never-read row does not get its artist and duration back then broken
  - if a user-renamed title is overwritten by the file's tag then broken
  - if a row with a duration is treated as never-read then broken
  - if the library file itself changes then broken
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import folder, tag_refresh
from apps.shared.state.writer import StateWriter
from tests.fixtures import tagged_audio as ta

pytestmark = [pytest.mark.requires_ffmpeg]


def _writer(conn) -> StateWriter:
    return StateWriter(conn, bus=FakeEventBus(), actor="tag-refresh-test")


def _imported_blank(state_conn, tmp_path: Path, *, title: str | None = None) -> tuple[str, Path]:
    """Import one tagged mp3, then blank its row the way a reader-less build wrote it."""
    root = tmp_path / "music"
    root.mkdir()
    path = ta.make_tagged_audio(root, "mp3-v24")
    writer = _writer(state_conn)
    try:
        folder.ingest_folder(writer, [root], dry_run=False)
    finally:
        writer.close()
    (sid,) = state_conn.execute("SELECT stable_id FROM tracks").fetchone()
    state_conn.execute(
        "UPDATE tracks SET title = ?, artists_json = '[]', album = NULL, duration_ms = NULL",
        (title if title is not None else Path(path).stem,),
    )
    state_conn.commit()
    return sid, Path(path)


def _row(state_conn) -> tuple[str, list[str], str | None, int | None]:
    title, artists, album, duration = state_conn.execute(
        "SELECT title, artists_json, album, duration_ms FROM tracks"
    ).fetchone()
    return title, json.loads(artists), album, duration


def test_a_never_read_row_gets_its_tags_back(state_conn, tmp_path: Path) -> None:
    sid, path = _imported_blank(state_conn, tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    rows = tag_refresh.blank_rows(state_conn)
    assert [r.stable_id for r in rows] == [sid]
    writer = _writer(state_conn)
    try:
        assert tag_refresh.refresh_row(writer, rows[0]) is True
    finally:
        writer.close()
    title, artists, album, duration = _row(state_conn)
    assert title == ta.STANDARD_TAGS["title"]
    assert artists == [ta.STANDARD_TAGS["artist"]], "if the artist does not come back then broken"
    assert album == ta.STANDARD_TAGS["album"]
    assert duration is not None and 900 <= duration <= 1200
    assert tag_refresh.blank_rows(state_conn) == [], "if a refreshed row stays a candidate then broken"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before, "if the library file changes then broken"


def test_a_user_renamed_title_is_kept(state_conn, tmp_path: Path) -> None:
    _imported_blank(state_conn, tmp_path, title="My Own Name")
    writer = _writer(state_conn)
    try:
        tag_refresh.refresh_row(writer, tag_refresh.blank_rows(state_conn)[0])
    finally:
        writer.close()
    title, artists, _album, _duration = _row(state_conn)
    assert title == "My Own Name", "if a user-renamed title is overwritten then broken"
    assert artists == [ta.STANDARD_TAGS["artist"]]


def test_a_row_with_a_duration_is_not_a_candidate(state_conn, tmp_path: Path) -> None:
    """Control: a normally imported row (duration present) is never re-read."""
    root = tmp_path / "music"
    root.mkdir()
    ta.make_tagged_audio(root, "mp3-v24")
    writer = _writer(state_conn)
    try:
        folder.ingest_folder(writer, [root], dry_run=False)
    finally:
        writer.close()
    assert tag_refresh.blank_rows(state_conn) == []


def test_tags_without_a_duration_land_but_report_failure(
    state_conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose tags read but whose audio frames do not (no duration)
    would stay a candidate forever and pin the drain on its tag phase, so the
    read must land what it got and still answer False.

    MUTATION TARGET: return True after any read and this answers True while
    the row is still selected by ``blank_rows``.
    """
    from dataclasses import replace

    sid, _path = _imported_blank(state_conn, tmp_path)
    real = tag_refresh.audio_files.read_metadata
    monkeypatch.setattr(
        tag_refresh.audio_files, "read_metadata",
        lambda path: (lambda md: md and replace(md, duration_s=None))(real(path)),
    )
    (row,) = tag_refresh.blank_rows(state_conn)
    writer = _writer(state_conn)
    try:
        assert tag_refresh.refresh_row(writer, row) is False
    finally:
        writer.close()
    _title, artists, _album, duration = _row(state_conn)
    assert artists == [ta.STANDARD_TAGS["artist"]] and duration is None
    assert [r.stable_id for r in tag_refresh.blank_rows(state_conn)] == [sid]
