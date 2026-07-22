"""Tests for ``apps.shared.tag_writer`` (Phase 7 Plan 02)."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from apps.shared.tag_writer import (
    POPM_BUCKETS,
    UnifiedTags,
    UnsupportedContainer,
    read_tags,
    write_tags,
)

# tag read/write needs the tags extra; skip (never fail) when absent.
pytestmark = pytest.mark.requires_mutagen


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _copy_fixture(tmp_path: Path, name: str) -> Path:
    src = FIXTURE_ROOT / name
    dst = tmp_path / name
    shutil.copy2(src, dst)
    return dst


@pytest.mark.requirement("META-01")
def test_popm_bucket_mapping_pinned() -> None:
    """Locked per the Rekordbox + Windows conventions."""
    assert POPM_BUCKETS == {0: 0, 1: 51, 2: 102, 3: 153, 4: 204, 5: 255}


@pytest.mark.requirement("META-01")
def test_unsupported_aiff_raises(tmp_path: Path) -> None:
    p = tmp_path / "foo.aiff"
    p.write_bytes(b"fake")
    with pytest.raises(UnsupportedContainer):
        read_tags(p)
    with pytest.raises(UnsupportedContainer):
        write_tags(p, UnifiedTags(title="x"), dry_run=True)


@pytest.mark.requirement("META-01")
def test_dry_run_no_mutation_mp3(tmp_path: Path) -> None:
    dst = _copy_fixture(tmp_path, "src-320.mp3")
    before = _sha(dst)
    write_tags(
        dst,
        UnifiedTags(title="Unified", artist="X", rating=3, bpm=128.0, energy=7),
        dry_run=True,
    )
    assert _sha(dst) == before, "dry-run changed the file"


@pytest.mark.requirement("META-01")
def test_mp3_roundtrip(tmp_path: Path) -> None:
    dst = _copy_fixture(tmp_path, "src-320.mp3")
    plan = UnifiedTags(
        title="Unified T",
        artist="Unified A",
        album="Unified Alb",
        genre="Techno",
        bpm=128.0,
        key_openkey="8d",
        key_camelot="8A",
        energy=7,
        rating=4,
        isrc="USRC17607839",
    )
    write_tags(dst, plan, dry_run=False)
    got = read_tags(dst)
    assert got.title == "Unified T"
    assert got.artist == "Unified A"
    assert got.album == "Unified Alb"
    assert got.genre == "Techno"
    assert got.bpm == 128.0
    assert got.key_openkey == "8d"
    assert got.key_camelot == "8A"
    assert got.energy == 7
    assert got.rating == 4
    assert got.isrc == "USRC17607839"


@pytest.mark.requirement("META-01")
def test_mp4_roundtrip(tmp_path: Path) -> None:
    dst = _copy_fixture(tmp_path, "src.m4a")
    plan = UnifiedTags(
        title="T",
        artist="A",
        album="Alb",
        genre="House",
        bpm=124.0,
        key_openkey="9m",
        key_camelot="9B",
        energy=6,
        rating=3,
        isrc="USRC17607839",
    )
    write_tags(dst, plan, dry_run=False)
    got = read_tags(dst)
    assert got.title == "T"
    assert got.artist == "A"
    assert got.genre == "House"
    assert got.bpm == 124.0
    assert got.key_openkey == "9m"
    assert got.key_camelot == "9B"
    assert got.energy == 6
    assert got.rating == 3
    assert got.isrc == "USRC17607839"


@pytest.mark.requirement("META-01")
def test_flac_roundtrip(tmp_path: Path) -> None:
    dst = _copy_fixture(tmp_path, "src.flac")
    plan = UnifiedTags(
        title="FT",
        artist="FA",
        genre="Ambient",
        bpm=60.0,
        key_openkey="1d",
        key_camelot="8B",
        energy=2,
        rating=5,
        isrc="USRC17607839",
    )
    write_tags(dst, plan, dry_run=False)
    got = read_tags(dst)
    assert got.title == "FT"
    assert got.artist == "FA"
    assert got.genre == "Ambient"
    assert got.bpm == 60.0
    assert got.key_openkey == "1d"
    assert got.key_camelot == "8B"
    assert got.energy == 2
    assert got.rating == 5


@pytest.mark.requirement("META-01")
def test_partial_plan_only_writes_specified_fields(tmp_path: Path) -> None:
    """None fields in UnifiedTags must not be written."""
    dst = _copy_fixture(tmp_path, "src-320.mp3")
    # First, write a full plan so there is existing data.
    write_tags(
        dst,
        UnifiedTags(title="Original", artist="OrigArt", genre="Orig"),
        dry_run=False,
    )
    # Now rewrite only genre.
    write_tags(dst, UnifiedTags(genre="Techno"), dry_run=False)
    got = read_tags(dst)
    assert got.title == "Original"
    assert got.artist == "OrigArt"
    assert got.genre == "Techno"


@pytest.mark.requirement("META-01")
def test_invalid_rating_raises(tmp_path: Path) -> None:
    dst = _copy_fixture(tmp_path, "src-320.mp3")
    with pytest.raises(ValueError):
        write_tags(dst, UnifiedTags(rating=9), dry_run=False)
