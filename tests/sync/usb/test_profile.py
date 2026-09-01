"""Tests for apps.sync.usb.profile."""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync.usb.profile import (
    CONFLICT_POLICIES,
    FORMATS,
    LAYOUTS,
    PLAYLIST_FILES,
    ProfileError,
    default_mount_point,
    load,
    load_from_string,
)

HAPPY = """\
name: gigA
drive_label: GIG-A
playlists:
  - Warmup
  - "Peak Time A"
format: copy-as-is
layout: "Artist/Album/Track"
playlist_files: m3u8
conflict_policy: canonical-wins
"""


@pytest.mark.requirement("CAT-02")
def test_happy_load_from_string() -> None:
    p = load_from_string(HAPPY)
    assert p.name == "gigA"
    assert p.drive_label == "GIG-A"
    assert p.playlists == ("Warmup", "Peak Time A")
    assert p.format == "copy-as-is"
    assert p.layout == "Artist/Album/Track"
    assert p.playlist_files == "m3u8"
    assert p.conflict_policy == "canonical-wins"
    assert p.needs_transcode is False
    assert default_mount_point(p.drive_label, platform="darwin").name == "GIG-A"


def test_default_mount_point_uses_macos_volume_root() -> None:
    assert default_mount_point("GIG-A", platform="darwin") == Path("/Volumes/GIG-A")


def test_default_mount_point_requires_explicit_root_on_windows() -> None:
    with pytest.raises(ProfileError, match="--drive-root is required on win32"):
        default_mount_point("GIG-A", platform="win32")


@pytest.mark.requirement("CAT-02")
def test_unknown_key_rejected() -> None:
    bad = HAPPY + "unknown_key: 42\n"
    with pytest.raises(ProfileError, match="unknown keys"):
        load_from_string(bad)


@pytest.mark.requirement("CAT-02")
def test_missing_playlist_rejected() -> None:
    bad = HAPPY.replace(
        "playlists:\n  - Warmup\n  - \"Peak Time A\"\n",
        "playlists: []\n",
    )
    with pytest.raises(ProfileError, match="non-empty"):
        load_from_string(bad)


@pytest.mark.requirement("CAT-02")
def test_bad_format_enum() -> None:
    bad = HAPPY.replace("format: copy-as-is", "format: flac@lossless")
    with pytest.raises(ProfileError, match="format"):
        load_from_string(bad)


@pytest.mark.requirement("CAT-02")
def test_bad_layout_enum() -> None:
    bad = HAPPY.replace('layout: "Artist/Album/Track"', "layout: weird")
    with pytest.raises(ProfileError, match="layout"):
        load_from_string(bad)


@pytest.mark.requirement("CAT-02")
def test_bad_conflict_policy() -> None:
    bad = HAPPY.replace("conflict_policy: canonical-wins", "conflict_policy: merge")
    with pytest.raises(ProfileError, match="conflict_policy"):
        load_from_string(bad)


@pytest.mark.requirement("CAT-02")
def test_enums_exposed_and_non_empty() -> None:
    assert "copy-as-is" in FORMATS
    assert "mp3@320" in FORMATS
    assert "Artist/Album/Track" in LAYOUTS
    assert "m3u8" in PLAYLIST_FILES
    assert "canonical-wins" in CONFLICT_POLICIES


@pytest.mark.requirement("CAT-02")
def test_load_round_trip(tmp_path) -> None:
    p = tmp_path / "p.yaml"
    p.write_text(HAPPY, encoding="utf-8")
    prof = load(p)
    assert prof.name == "gigA"


@pytest.mark.requirement("CAT-02")
def test_needs_transcode_and_exclusions() -> None:
    yaml_str = HAPPY.replace("format: copy-as-is", "format: mp3@320")
    yaml_str += "exclusions:\n  - samples/\n"
    prof = load_from_string(yaml_str)
    assert prof.needs_transcode is True
    assert "samples/" in prof.exclusions


@pytest.mark.requirement("CAT-02")
def test_required_missing_keys_report_all() -> None:
    with pytest.raises(ProfileError, match="missing required keys"):
        load_from_string("name: only\n")


@pytest.mark.requirement("CAT-02")
def test_root_must_be_mapping() -> None:
    with pytest.raises(ProfileError, match="mapping"):
        load_from_string("- justlist\n")
