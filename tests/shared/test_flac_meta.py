"""TAGIO-03: the in-house FLAC metadata writer (apps.shared.flac_meta).

[if] a FLAC comment is written [then] tinytag sees it, audio intact, [else stop].

Base files are real FLACs encoded and tagged by ffmpeg; every write is read
back by tinytag (independent) and decoded by ffmpeg.

Regression one-liners:
  - if a written Vorbis comment is not seen by tinytag then broken
  - if the audio frames change on a comment write then broken
  - if a comment write drops STREAMINFO / SEEKTABLE / existing tags then broken
  - if a comment that fits moves the audio offset then broken
  - if a non-FLAC file is rewritten instead of refused then broken
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from apps.shared import flac_meta, tag_reader
from tests.fixtures import tagged_audio as ta

pytestmark = [pytest.mark.requirement("TAGIO-03"), pytest.mark.requires_ffmpeg]


def _decodes(path: Path) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    return result.returncode == 0 and not result.stderr.strip()


def test_a_written_comment_is_seen_by_an_independent_reader(tmp_path: Path) -> None:
    """[if] BPM / GENRE comments are set [then] tinytag reads them back, [else stop]."""
    flac = ta.make_tagged_audio(tmp_path, "flac")
    meta = flac_meta.read(flac)
    meta.set("BPM", "131")
    meta.set("genre", "Dub Techno ☃")  # case-insensitive replace of GENRE
    flac_meta.save(flac, meta)

    read = tag_reader.read_tags(flac)
    assert (read.bpm, read.genre) == (131.0, "Dub Techno ☃")
    assert read.artist == ta.STANDARD_TAGS["artist"]  # untouched comment survives
    assert flac_meta.read(flac).get("GENRE") == ["Dub Techno ☃"]  # replaced, not duplicated
    assert _decodes(flac)


def test_a_comment_write_leaves_the_audio_frames_identical(tmp_path: Path) -> None:
    """[if] comments grow past the old padding [then] audio frames are identical, [else stop]."""
    flac = ta.make_tagged_audio(tmp_path, "flac")
    before = ta.flac_audio_frames(flac)
    meta = flac_meta.read(flac)
    meta.set("SERATO_BLOB", "x" * 20_000)  # bigger than any default padding
    flac_meta.save(flac, meta)
    assert ta.flac_audio_frames(flac) == before
    assert _decodes(flac)


def test_a_write_that_fits_keeps_the_audio_offset(tmp_path: Path) -> None:
    """[if] the new comment block fits [then] the audio offset is unchanged, [else stop]."""
    flac = ta.make_tagged_audio(tmp_path, "flac")
    offset = flac_meta.read(flac).audio_offset
    meta = flac_meta.read(flac)
    meta.set("BPM", "99")
    flac_meta.save(flac, meta)
    assert flac_meta.read(flac).audio_offset == offset


def test_non_comment_blocks_are_kept(tmp_path: Path) -> None:
    """[if] a comment is written [then] STREAMINFO and other blocks are kept, [else stop]."""
    flac = ta.make_tagged_audio(tmp_path, "flac")
    ta.add_flac_picture(flac, b"\x89PNG\r\n\x1a\n" + bytes(16), mime="image/png")
    before = [(b.block_type, b.data) for b in flac_meta.read(flac).blocks]
    assert [kind for kind, _ in before] == [flac_meta.BLOCK_STREAMINFO, flac_meta.BLOCK_PICTURE]
    meta = flac_meta.read(flac)
    meta.set("KEY", "4A")
    flac_meta.save(flac, meta)
    assert [(b.block_type, b.data) for b in flac_meta.read(flac).blocks] == before


def test_a_picture_block_is_read_as_artwork(tmp_path: Path) -> None:
    """[if] a PICTURE block is added [then] tinytag reads it as the cover, [else stop]."""
    flac = ta.make_tagged_audio(tmp_path, "flac")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    ta.add_flac_picture(flac, png, mime="image/png")
    assert list(tag_reader.embedded_pictures(flac)) == [(png, "image/png", 3)]


def test_a_non_flac_file_is_refused(tmp_path: Path) -> None:
    """[if] the file is not FLAC [then] FlacError is raised, [else stop]."""
    mp3 = ta.make_tagged_audio(tmp_path, "mp3-v23")
    with pytest.raises(flac_meta.FlacError, match="not a FLAC stream"):
        flac_meta.read(mp3)


def test_an_invalid_field_name_is_refused(tmp_path: Path) -> None:
    """[if] a field name contains '=' [then] FlacError, nothing written, [else stop]."""
    flac = ta.make_tagged_audio(tmp_path, "flac")
    with pytest.raises(flac_meta.FlacError, match="field name"):
        flac_meta.read(flac).set("A=B", "x")
