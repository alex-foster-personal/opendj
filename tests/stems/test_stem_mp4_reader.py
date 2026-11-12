"""Acceptance tests for NI ``.stem.mp4`` four-part reader (issue #12233).

[if] stem mp4 fixtures are exercised [then] ordered parts and metadata hold, [else stop].
"""

from __future__ import annotations

import shutil
import wave
from io import BytesIO
from pathlib import Path

import pytest

from apps.stems.stem_mp4 import (
    STEM_MP4_PART_IDS,
    StemMp4Error,
    decode_stem_mp4_part,
    read_stem_mp4,
)
from tests.stems.fixtures.build_stem_mp4_fixture import (
    build_three_part_stem_mp4,
    build_valid_four_part_stem_mp4,
)

pytestmark = [
    pytest.mark.requirement("RB-PERFORMANCE-13517"),
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg and ffprobe are required to build and probe stem mp4 fixtures",
    ),
]


@pytest.fixture
def valid_fixture(tmp_path: Path) -> Path:
    return build_valid_four_part_stem_mp4(tmp_path / "valid.stem.mp4")


def test_parts_follow_documented_container_order(valid_fixture: Path) -> None:
    """[if] a four-part fixture is parsed [then] parts match container order, [else stop]."""
    bundle = read_stem_mp4(valid_fixture)
    assert tuple(p.part_id for p in bundle.parts) == STEM_MP4_PART_IDS


def test_metadata_tags_and_stem_atom_are_retained(valid_fixture: Path) -> None:
    """[if] metadata tags exist [then] the reader returns track and stem metadata, [else stop]."""
    bundle = read_stem_mp4(valid_fixture)
    assert bundle.metadata.title == "Fixture Stem Track"
    assert bundle.metadata.artist == "Open DJ Tests"
    assert bundle.metadata.stem_atom_version == 1
    assert tuple(s.name for s in bundle.metadata.stems) == STEM_MP4_PART_IDS


def test_each_stem_part_decodes_to_non_silent_pcm(valid_fixture: Path) -> None:
    """[if] decode runs for each stem [then] PCM wav bytes are returned, [else stop]."""
    for part_id in STEM_MP4_PART_IDS:
        wav_bytes = decode_stem_mp4_part(valid_fixture, part_id)
        with wave.open(BytesIO(wav_bytes), "rb") as handle:
            assert handle.getnchannels() == 2
            assert handle.getframerate() == 44_100
            assert handle.getnframes() > 0


def test_three_stream_container_is_refused(tmp_path: Path) -> None:
    """[if] fewer than four stem streams [then] the reader refuses, [else stop]."""
    path = build_three_part_stem_mp4(tmp_path / "three.stem.mp4")
    with pytest.raises(StemMp4Error, match="four stem"):
        read_stem_mp4(path)


def test_corrupt_container_does_not_yield_bundle(tmp_path: Path) -> None:
    """[if] ffprobe fails on garbage [then] no usable bundle is returned, [else stop]."""
    path = tmp_path / "garbage.stem.mp4"
    path.write_bytes(b"not-an-mp4")
    with pytest.raises(StemMp4Error):
        read_stem_mp4(path)
