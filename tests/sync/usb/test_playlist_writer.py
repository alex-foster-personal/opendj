"""Tests for apps.sync.usb.playlist_writer."""
from __future__ import annotations

import pytest

from apps.sync.usb.playlist_writer import PLAYLISTS_DIRNAME, write_m3u8s
from apps.sync.usb.profile import load_from_string
from apps.sync.usb.state import group_by_playlist


def _profile_with_file_mode(mode: str):
    return load_from_string(
        f"""
name: fx
drive_label: FIXTURE-A
playlists: [Warmup, Peak]
format: copy-as-is
layout: "Artist/Album/Track"
playlist_files: {mode}
conflict_policy: canonical-wins
"""
    )


@pytest.mark.requirement("CAT-02")
def test_basic_write(drive_root, fixture_canonical) -> None:
    profile = _profile_with_file_mode("m3u8")
    by_pl = group_by_playlist(fixture_canonical)
    written = write_m3u8s(
        profile=profile, tracks_by_playlist=by_pl, drive_root=drive_root
    )
    assert len(written) == 2
    warmup = drive_root / PLAYLISTS_DIRNAME / "Warmup.m3u8"
    assert warmup.exists()
    text = warmup.read_text(encoding="utf-8")
    assert "#EXTM3U" in text
    assert text.count("#EXTINF") == 2  # two tracks in Warmup
    # Paths use ../ because playlists live under /Playlists/.
    assert "../Alice/AA/One.mp3" in text


@pytest.mark.requirement("CAT-02")
def test_sanitises_playlist_names(drive_root, make_track) -> None:
    tracks = [make_track(title="T", artist="A", album="B", playlist="Peak/Time A")]
    profile = load_from_string(
        """
name: fx
drive_label: FIXTURE-A
playlists: ["Peak/Time A"]
format: copy-as-is
layout: "Artist/Album/Track"
playlist_files: m3u8
conflict_policy: canonical-wins
"""
    )
    written = write_m3u8s(
        profile=profile,
        tracks_by_playlist=group_by_playlist(tracks),
        drive_root=drive_root,
    )
    assert len(written) == 1
    assert written[0].name == "Peak_Time A.m3u8"


@pytest.mark.requirement("CAT-02")
def test_playlist_files_none_skips(drive_root, fixture_canonical) -> None:
    profile = _profile_with_file_mode("none")
    out = write_m3u8s(
        profile=profile,
        tracks_by_playlist=group_by_playlist(fixture_canonical),
        drive_root=drive_root,
    )
    assert out == []
    assert not (drive_root / PLAYLISTS_DIRNAME).exists()
