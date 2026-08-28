"""Tests for apps.spotify.watched registry."""
from __future__ import annotations

from pathlib import Path

from apps.spotify.watched import (
    DEFAULT_WATCHED,
    WatchedPlaylist,
    ensure_watched_defaults,
    load_watched,
    save_watched,
    watched_ids,
)


def test_ensure_defaults_creates_four_otlf_urls(tmp_path: Path) -> None:
    path = tmp_path / "watched-playlists.json"
    entries = ensure_watched_defaults(path)
    assert path.is_file()
    assert len(entries) == len(DEFAULT_WATCHED)
    assert [e.id for e in entries] == [pid for pid, _url, _label in DEFAULT_WATCHED]
    # Idempotent.
    again = ensure_watched_defaults(path)
    assert [e.id for e in again] == [e.id for e in entries]


def test_save_load_roundtrip_preserves_custom(tmp_path: Path) -> None:
    path = tmp_path / "watched-playlists.json"
    custom = [
        WatchedPlaylist(
            id="abc123XYZ0",
            url="https://open.spotify.com/playlist/abc123XYZ0",
            label="custom",
            notes="keep",
        )
    ]
    save_watched(custom, path)
    loaded = load_watched(path)
    assert loaded == custom
    # ensure adds defaults without dropping custom
    merged = ensure_watched_defaults(path)
    assert merged[0].id == "abc123XYZ0"
    assert {e.id for e in merged} >= {pid for pid, *_ in DEFAULT_WATCHED}


def test_watched_ids_helper(tmp_path: Path) -> None:
    path = tmp_path / "watched-playlists.json"
    ids = watched_ids(path)
    assert ids == [pid for pid, *_ in DEFAULT_WATCHED]
