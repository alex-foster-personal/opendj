"""LIBM-178: the library listings and AutoPlay's candidates leave non-music out.

[if] a listing serves rekordbox Sampler rows without include_non_music [then] fail, [else stop].

the maintainer, Tue 6 Oct 2026: "This isn't a music track - I thought we auto-filtered
non-music out of the library?" (a 13 s GROOVE CIRCUIT preset). ``GET /tracks`` and ``GET /tracks/index`` hide such rows and count
them in ``non_music_hidden``; ``include_non_music=true`` serves them, and no row
is ever deleted. AutoPlay has no server-side picker: its candidate feed is the
view the browser resolves off ``GET /tracks/index`` (autoplay-feed.ts), so the
index is the AutoPlay candidate source tested here.

Regression one-liners:
  - if a default listing or index carries a Sampler row then broken
  - if include_non_music=true does not serve every row, or the count is off, then broken
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("LIBM-178")

STAMP = "2026-10-06T00:00:00Z"
SAMPLER = "/Users/dev/Music/rekordbox/Sampler"
#: stable_id -> (title, file_path, genre or None)
ROWS = {
    "a" * 40: ("Song", "/Users/dev/Music/House/Song.mp3", "House"),
    "b" * 40: ("Quillfen Loop Nine", f"{SAMPLER}/GROOVE CIRCUIT/PRESET/Quillfen Kit/Quillfen Loop Nine.wav", None),
    "c" * 40: ("MERGE FX 01", f"{SAMPLER}/MERGE FX/Riser/MERGE FX 01.wav", None),
    "d" * 40: ("Loop", "/Volumes/USB/loops/Loop.wav", "Loop Samples"),
    "e" * 40: ("Intro", "/Users/dev/Music/Edits/Intro.mp3", "House"),
}
MUSIC = {"a" * 40, "e" * 40}


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    try:
        for sid, (title, file_path, _genre) in ROWS.items():
            writer.upsert_track(
                stable_id=sid, stable_id_tier="inferred", title=title, artists=["rekordbox"],
                album=None, isrc=None, duration_ms=13_000, file_path=file_path,
            )
    finally:
        writer.close()
        conn.close()
    raw = sqlite3.connect(path)
    try:
        raw.executemany(
            "INSERT INTO track_fields (stable_id, field_name, value_json, source, modified_at) "
            "VALUES (?, 'genre', ?, 'rekordbox', ?)",
            [(sid, f'"{genre}"', STAMP) for sid, (_t, _p, genre) in ROWS.items() if genre],
        )
        raw.commit()
    finally:
        raw.close()
    monkeypatch.setattr(rb_config, "STATE_DB", path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent-master.db")
    monkeypatch.setattr(rb_config, "LOCAL_WAVEFORM_CACHE_DIR", tmp_path / "wf-cache")
    return path


def _get(path: Path, url: str) -> dict:
    app = create_app()
    app.state.backend = SqliteBackend(path)
    app.state.state_db_path = str(path)
    app.state.analysis_db_path = str(path)
    with TestClient(app) as client:
        response = client.get(url)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("url", ["/api/v1/tracks", "/api/v1/tracks/index"])
def test_listing_hides_non_music_by_default(state_path: Path, url: str) -> None:
    """[if] a default listing [then] only music rows, hidden count 3, [else stop]."""
    body = _get(state_path, url)
    assert {row["stable_id"] for row in body["items"]} == MUSIC
    assert body["non_music_hidden"] == len(ROWS) - len(MUSIC)


@pytest.mark.parametrize("url", ["/api/v1/tracks", "/api/v1/tracks/index"])
def test_include_non_music_serves_every_row(state_path: Path, url: str) -> None:
    """[if] include_non_music=true [then] every row and hidden count 0, [else stop]."""
    body = _get(state_path, f"{url}?include_non_music=true")
    assert {row["stable_id"] for row in body["items"]} == set(ROWS)
    assert body["non_music_hidden"] == 0


def test_autoplay_candidate_source_never_carries_a_sampler_row(state_path: Path) -> None:
    """[if] AutoPlay's index source carries non-music [then] fail, [else stop]."""
    titles = {row["title"] for row in _get(state_path, "/api/v1/tracks/index")["items"]}
    assert titles == {"Song", "Intro"}
    assert "Quillfen Loop Nine" not in titles
