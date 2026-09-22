"""Shared fixtures for Phase 10 USB sync tests.

No real USB is ever required. We fabricate:

* A :mod:`tmp_path`-rooted drive directory.
* A small set of "source" audio files (tiny .mp3 payloads with unique
  bytes) that live in a sibling fixture dir.
* A list of :class:`apps.sync.usb.state.CanonicalTrack` entries built
  directly (no Rekordbox DB needed; callers that want to exercise the
  RB shim use a custom fake DB).
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from apps.sync.usb.profile import Profile, load_from_string
from apps.sync.usb.state import CanonicalTrack

PROFILE_YAML = """\
name: fixtureA
drive_label: FIXTURE-A
playlists:
  - Warmup
  - Peak
format: copy-as-is
layout: "Artist/Album/Track"
playlist_files: m3u8
conflict_policy: canonical-wins
exclusions: []
"""


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _content_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def fixture_profile(tmp_path: Path) -> Profile:
    """Base profile whose drive_label matches the drive tmp dir."""
    return load_from_string(PROFILE_YAML)


@pytest.fixture
def drive_root(tmp_path: Path) -> Path:
    """A drive-root directory whose name matches the profile drive_label."""
    root = tmp_path / "FIXTURE-A"
    root.mkdir()
    return root


@pytest.fixture
def sources_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sources"
    d.mkdir()
    return d


@pytest.fixture
def make_track(sources_dir: Path) -> Callable[..., CanonicalTrack]:
    """Factory: build a CanonicalTrack with a real backing bytes file."""
    counter = {"n": 0}

    def _make(
        *,
        title: str,
        artist: str = "Artist",
        album: str = "Album",
        playlist: str = "Warmup",
        payload: bytes | None = None,
        stable_id: str | None = None,
    ) -> CanonicalTrack:
        counter["n"] += 1
        idx = counter["n"]
        payload = payload if payload is not None else f"audio-{idx}-{title}".encode()
        src = sources_dir / f"{artist}/{album}/{title}.mp3"
        _write_bytes(src, payload)
        return CanonicalTrack(
            stable_id=stable_id or f"sid-{idx}",
            playlist=playlist,
            source_path=src,
            title=title,
            artist=artist,
            album=album,
            duration_ms=120_000,
            size_bytes=len(payload),
            isrc=None,
            content_hash=_content_hash(payload),
        )

    return _make


@pytest.fixture
def fixture_canonical(make_track) -> list[CanonicalTrack]:
    """Three tracks, two in Warmup, one in Peak."""
    return [
        make_track(title="One", artist="Alice", album="AA", playlist="Warmup"),
        make_track(title="Two", artist="Bob", album="BB", playlist="Warmup"),
        make_track(title="Three", artist="Carol", album="CC", playlist="Peak"),
    ]
