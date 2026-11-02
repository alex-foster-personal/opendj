"""Shared JEV fixtures: the captured replies and a small library, for the genre suite and
the web UI's genre-guess routes (tests/webui/test_genre_guess_routes.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apps.genre_infer import jev
from tests.genre_infer.captured_fixture import load_captured
from tests.webui.library_wheel_fixtures import _make_state_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "jev"
WARMUP = jev.TagQuestion("warmup", "Is this a warm-up track?")
MANIFEST_VERSION = 1


def captured(name: str) -> dict[str, Any]:
    """A captured reply, verified against the fixture manifest before it is parsed."""
    return load_captured(FIXTURES, name, manifest_version=MANIFEST_VERSION)


def library(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    state = data / "state" / "state.db"
    state.parent.mkdir(parents=True)
    _make_state_db(
        state,
        tracks=[
            {"stable_id": "t-tagged", "title": "Tagged", "artists": ["A"], "file_path": "/m/a.mp3"},
            {"stable_id": "t-unmapped", "title": "Unmapped", "artists": ["C"], "file_path": "/m/c.mp3"},
            {"stable_id": "t-bare", "title": "Bare", "artists": ["B"], "file_path": "/m/AVIRA.mp3"},
            {"stable_id": "t-win", "title": "Win", "artists": ["W"], "file_path": "C:\\Users\\dev\\Music\\track.mp3"},
            {"stable_id": "t-blank", "title": "Blank", "artists": ["K"], "file_path": "/m/k.mp3"},
        ],
        memberships=[],
        playlists=[],
        vendor_ids={},
        track_fields=[
            {"stable_id": "t-tagged", "field_name": "genre", "value_json": '"House"'},
            # A tag the wheel maps to no family is still a tag: never asked.
            {"stable_id": "t-unmapped", "field_name": "genre", "value_json": '"Classical"'},
            {"stable_id": "t-bare", "field_name": "bpm", "value_json": "128"},
            # A whitespace-only genre is no tag: asked, and its guess is served.
            {"stable_id": "t-blank", "field_name": "genre", "value_json": '"   "'},
        ],
    )
    return data, state
