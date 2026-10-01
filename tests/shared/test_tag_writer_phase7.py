"""Tests for ``apps.shared.tag_writer`` (Phase 7 Plan 02)."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from apps.shared.tag_writer import (
    POPM_BUCKETS,
    TagRead,
    UnifiedTags,
    UnsupportedContainer,
    read_tags,
    write_tags,
)

from tests.fixtures import tagged_audio as ta

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


_FULL_PLAN = UnifiedTags(
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


def _assert_full_plan(got: TagRead) -> None:
    assert (got.title, got.artist, got.album, got.genre) == ("Unified T", "Unified A", "Unified Alb", "Techno")
    assert (got.bpm, got.key_openkey, got.key_camelot) == (128.0, "8d", "8A")
    assert (got.energy, got.rating, got.isrc) == (7, 4, "USRC17607839")


@pytest.mark.requirement("TAGIO-05")
@pytest.mark.requires_ffmpeg
def test_mp4_roundtrip_keeps_the_audio(tmp_path: Path) -> None:
    """[if] a full plan is written to an .m4a [then] every field reads back, audio identical, [else stop]."""
    dst = _copy_fixture(tmp_path, "src.m4a")
    pcm = ta.decoded_audio_sha256(dst)
    before = _sha(dst)
    write_tags(dst, _FULL_PLAN, dry_run=True)
    assert _sha(dst) == before, "dry-run changed the file"
    result = write_tags(dst, _FULL_PLAN, dry_run=False)
    assert set(result.applied) == {f for f in UnifiedTags.__dataclass_fields__ if getattr(_FULL_PLAN, f) is not None}
    _assert_full_plan(read_tags(dst))
    probed = ta.ffprobe_tags(dst)
    assert (probed["initialkey"], probed["camelot"], probed["isrc"], probed["rating"]) == ("8d", "8A", "USRC17607839", "4")
    assert ta.decoded_audio_sha256(dst) == pcm


@pytest.mark.requirement("TAGIO-05")
@pytest.mark.requires_ffmpeg
@pytest.mark.parametrize("codec", ["ogg", "opus"])
def test_ogg_roundtrip_keeps_the_audio(tmp_path: Path, codec: str) -> None:
    """[if] a full plan is written to Ogg Vorbis / Opus [then] every field reads back, audio identical, [else stop]."""
    # No INITIALKEY in the base file: tinytag prefers it over KEY, the field
    # this writer (like the mutagen one before it) uses for the Open Key value.
    dst = ta.make_tagged_audio(tmp_path, codec, tags=ta.STANDARD_TAGS)
    pcm = ta.decoded_audio_sha256(dst)
    write_tags(dst, _FULL_PLAN, dry_run=False)
    _assert_full_plan(read_tags(dst))
    probed = ta.ffprobe_tags(dst)
    assert (probed["bpm"], probed["key"], probed["camelot"], probed["rating"]) == ("128", "8d", "8A", "4")
    assert ta.decoded_audio_sha256(dst) == pcm


@pytest.mark.requirement("TAGIO-01")
@pytest.mark.requires_ffmpeg
def test_mp4_custom_atoms_are_read(tmp_path: Path) -> None:
    """[if] an m4a carries tags and iTunes atoms [then] read_tags returns them, [else stop]."""
    dst = tmp_path / "tagged.m4a"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-i", str(FIXTURE_ROOT / "src.m4a"), "-c", "copy",
            "-metadata", "title=T", "-metadata", "genre=House", "-metadata", "tmpo=124",
            str(dst),
        ],
        check=True,
    )
    got = read_tags(dst)
    assert (got.title, got.genre, got.bpm) == ("T", "House", 124.0)


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
