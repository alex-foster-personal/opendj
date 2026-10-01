"""TAGIO-02: the in-house ID3v2 reader / writer (apps.shared.id3v2).

[if] an ID3 frame is written [then] tinytag sees it, audio intact, [else stop].

Every write is checked by tinytag (an independent reader) and, for the audio
bytes, by comparing the stream after the tag byte-for-byte; the base files are
real MP3s encoded and tagged by ffmpeg.

Regression one-liners:
  - if a written text / TXXX / COMM / GEOB frame is not seen by tinytag then broken
  - if the audio after the tag changes on a tag write then broken
  - if a frame this module did not touch is altered or dropped then broken
  - if a write that fits the old tag moves the audio offset then broken
  - if a failed write leaves a partial file then broken
  - if a v2.4 unsynchronised frame is not decoded then broken
  - if a v2.2 tag is silently rewritten instead of refused then broken
"""
from __future__ import annotations

import struct
import subprocess
from pathlib import Path

import pytest

from apps.shared import file_rewrite, id3v2, tag_reader
from tests.fixtures import tagged_audio as ta

pytestmark = [pytest.mark.requirement("TAGIO-02"), pytest.mark.requires_ffmpeg]


def _tag(path: Path) -> id3v2.Id3Tag:
    tag = id3v2.read_tag(path)
    assert tag is not None, f"{path} has no ID3v2 tag"
    return tag


def _decodes(path: Path) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    )
    return result.returncode == 0 and not result.stderr.strip()


@pytest.mark.parametrize("fmt", ["mp3-v23", "mp3-v24"])
def test_written_frames_are_seen_by_an_independent_reader(tmp_path: Path, fmt: str) -> None:
    """[if] text/TXXX/COMM frames are written [then] tinytag reads them back, [else stop]."""
    mp3 = ta.make_tagged_audio(tmp_path, fmt)
    tag = id3v2.load_or_new(mp3)
    tag.set_text("TBPM", "128")
    tag.set_text("TCON", "Tëchno ☃")
    tag.set_txxx("CAMELOT", "9A")
    tag.frames.append(id3v2.Frame("COMM", id3v2.encode_comm("eng", "", "über peak ☃", tag.version)))
    id3v2.save(mp3, tag)

    read = tag_reader.read_tags(mp3)
    assert (read.bpm, read.genre, read.comment) == (128.0, "Tëchno ☃", "über peak ☃")
    assert read.first_other("camelot") == "9A"
    assert read.artist == ta.STANDARD_TAGS["artist"]  # untouched frame survives
    assert _tag(mp3).version == int(fmt[-1])  # version is kept
    assert _decodes(mp3)


def test_a_tag_write_leaves_the_audio_bytes_identical(tmp_path: Path) -> None:
    """[if] a tag is rewritten [then] the audio after it is byte-identical, [else stop]."""
    mp3 = ta.make_tagged_audio(tmp_path, "mp3-v23")
    before = ta.audio_after_id3(mp3)
    tag = id3v2.load_or_new(mp3)
    tag.set_geob("Serato Markers2", b"\x01\x01" + bytes(range(256)) * 40)  # grows the tag
    id3v2.save(mp3, tag)
    assert ta.audio_after_id3(mp3) == before


def test_a_file_with_no_tag_gets_one_and_keeps_its_audio(tmp_path: Path) -> None:
    """[if] an mp3 has no ID3 tag [then] a v2.3 tag is added before the audio, [else stop]."""
    mp3 = ta.make_untagged_audio(tmp_path, "mp3-v23")
    raw = mp3.read_bytes()
    if raw[:3] == b"ID3":  # ffmpeg may still emit an empty tag; strip it for this case
        raw = raw[_tag(mp3).original_size:]
        mp3.write_bytes(raw)
    assert id3v2.read_tag(mp3) is None
    tag = id3v2.load_or_new(mp3)
    tag.set_text("TIT2", "fresh")
    id3v2.save(mp3, tag)
    assert _tag(mp3).version == 3
    assert ta.audio_after_id3(mp3) == raw
    assert tag_reader.read_tags(mp3).title == "fresh"


def test_untouched_frames_are_preserved_byte_for_byte(tmp_path: Path) -> None:
    """[if] one frame is upserted [then] every other frame keeps its bytes, [else stop]."""
    mp3 = ta.make_tagged_audio(tmp_path, "mp3-v24")
    original = {(f.frame_id, f.data) for f in _tag(mp3).frames}
    tag = id3v2.load_or_new(mp3)
    tag.set_text("TKEY", "11B")
    id3v2.save(mp3, tag)
    after = {(f.frame_id, f.data) for f in _tag(mp3).frames}
    assert {f for f in original if f[0] != "TKEY"} <= after


def test_a_write_that_fits_keeps_the_audio_offset(tmp_path: Path) -> None:
    """[if] new frames fit inside the old tag [then] its size is unchanged, [else stop]."""
    mp3 = ta.make_tagged_audio(tmp_path, "mp3-v23")
    tag = id3v2.load_or_new(mp3)
    tag.set_text("TIT2", "a much longer title that forces a rewrite with padding" * 4)
    id3v2.save(mp3, tag)
    padded_size = _tag(mp3).original_size
    tag = id3v2.load_or_new(mp3)
    tag.set_text("TIT2", "short")
    id3v2.save(mp3, tag)
    assert _tag(mp3).original_size == padded_size


def test_geob_round_trips_with_serato_layout(tmp_path: Path) -> None:
    """[if] a GEOB frame is upserted [then] it reads back once, latin-1 header, [else stop]."""
    mp3 = ta.make_tagged_audio(tmp_path, "mp3-v23")
    payload = b"\x01\x01" + b"\xff\x00\xff" * 10  # bytes that need unsync if unsynchronised
    for _ in range(2):  # an upsert twice must leave ONE frame
        tag = id3v2.load_or_new(mp3)
        tag.set_geob("Serato Markers2", payload)
        id3v2.save(mp3, tag)
    frames = [f for f in _tag(mp3).frames if f.frame_id == "GEOB"]
    assert len(frames) == 1
    assert frames[0].data.startswith(b"\x00application/octet-stream\x00\x00Serato Markers2\x00")
    assert _tag(mp3).geob() == [("application/octet-stream", "", "Serato Markers2", payload)]


def test_popm_rating_round_trips(tmp_path: Path) -> None:
    """[if] a POPM rating is written [then] the same byte reads back, [else stop]."""
    mp3 = ta.make_tagged_audio(tmp_path, "mp3-v23")
    tag = id3v2.load_or_new(mp3)
    tag.set_popm("x@local", 204)
    id3v2.save(mp3, tag)
    assert _tag(mp3).popm_rating() == 204


def test_a_failed_write_leaves_the_original_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] writing fails midway [then] the original file is byte-identical, [else stop]."""
    mp3 = ta.make_tagged_audio(tmp_path, "mp3-v23")
    before = mp3.read_bytes()
    real_fsync = file_rewrite.os.fsync

    def failing_fsync(fd: int) -> None:
        real_fsync(fd)
        raise OSError("disk full")

    monkeypatch.setattr(file_rewrite.os, "fsync", failing_fsync)
    tag = id3v2.load_or_new(mp3)
    tag.set_text("TIT2", "never lands")
    with pytest.raises(OSError, match="disk full"):
        id3v2.save(mp3, tag)
    assert mp3.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [mp3.name]  # no temp file left


def _raw_tag(version: int, frames: bytes, flags: int = 0) -> bytes:
    size = len(frames)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3" + bytes([version, 0, flags]) + syncsafe + frames


def test_a_v24_unsynchronised_frame_is_decoded(tmp_path: Path) -> None:
    """[if] a v2.4 frame has the unsync flag [then] its body is decoded, [else stop]."""
    body = b"\x00" + b"A\xff\x00\xe0B"  # latin-1 text "A\xffxe0B" stored unsynchronised
    size = bytes([0, 0, 0, len(body)])
    frame = b"TIT2" + size + b"\x00\x02" + body
    mp3 = tmp_path / "u.mp3"
    mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
    tag = _tag(mp3)
    assert tag.frames[0].data == b"\x00A\xff\xe0B"
    assert tag.frames[0].format_flags == 0


def test_a_v22_tag_is_refused_not_rewritten(tmp_path: Path) -> None:
    """[if] the tag is ID3v2.2 [then] Id3Error is raised and nothing written, [else stop]."""
    mp3 = tmp_path / "old.mp3"
    original = _raw_tag(2, b"TT2" + b"\x00\x00\x03" + b"\x00hi") + b"\xff\xfb\x90\x00" * 4
    mp3.write_bytes(original)
    with pytest.raises(id3v2.Id3Error, match="v2.2"):
        id3v2.load_or_new(mp3)
    assert mp3.read_bytes() == original


def test_a_frame_running_past_the_tag_is_an_error(tmp_path: Path) -> None:
    """[if] a frame claims more bytes than the tag holds [then] Id3Error, [else stop]."""
    frame = b"TIT2" + struct.pack(">I", 500) + b"\x00\x00" + b"\x00short"
    mp3 = tmp_path / "bad.mp3"
    mp3.write_bytes(_raw_tag(3, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="runs past"):
        _tag(mp3)


def test_an_opaque_compressed_frame_is_kept_verbatim(tmp_path: Path) -> None:
    """[if] a frame is compressed [then] it is re-emitted unchanged, [else stop]."""
    compressed_body = b"\x00\x00\x00\x05" + b"zlibdata"
    frame = b"TXXX" + struct.pack(">I", len(compressed_body)) + b"\x00\x80" + compressed_body
    mp3 = tmp_path / "c.mp3"
    mp3.write_bytes(_raw_tag(3, frame) + b"\xff\xfb\x90\x00" * 4)
    tag = id3v2.load_or_new(mp3)
    tag.set_text("TIT2", "added")
    id3v2.save(mp3, tag)
    kept = [f for f in _tag(mp3).frames if f.frame_id == "TXXX"]
    assert [(f.format_flags, f.data) for f in kept] == [(0x80, compressed_body)]
