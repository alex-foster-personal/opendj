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
        capture_output=True, text=True, timeout=60, check=False,
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
    with pytest.raises(id3v2.Id3Error, match=r"v2\.2"):
        id3v2.load_or_new(mp3)
    assert mp3.read_bytes() == original


@pytest.mark.parametrize(("version", "flags"), [(3, 0x01), (3, 0x1F), (4, 0x01), (4, 0x0F)])
def test_a_reserved_header_flag_bit_is_an_error(tmp_path: Path, version: int, flags: int) -> None:
    """[if] a header sets a flag bit its version reserves [then] Id3Error, [else stop]."""
    mp3 = tmp_path / "flags.mp3"
    mp3.write_bytes(_raw_tag(version, b"", flags) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="reserved flag bits"):
        _tag(mp3)


@pytest.mark.parametrize(("version", "flags"), [(3, 0x20), (4, 0x10)])
def test_a_defined_header_flag_bit_is_not_an_error(tmp_path: Path, version: int, flags: int) -> None:
    """[if] a header sets only flag bits its version defines [then] it reads, [else stop]."""
    mp3 = tmp_path / "defined.mp3"
    footer = b"3DI\x04\x00" + bytes([flags]) + b"\x00\x00\x00\x00" if version == 4 else b""
    mp3.write_bytes(_raw_tag(version, b"", flags) + footer + b"\xff\xfb\x90\x00" * 4)
    assert _tag(mp3).version == version


def test_a_v24_footer_flag_without_a_footer_is_an_error(tmp_path: Path) -> None:
    """[if] a v2.4 header sets the footer flag but no 3DI footer follows [then] Id3Error, [else stop]."""
    mp3 = tmp_path / "no-footer.mp3"
    mp3.write_bytes(_raw_tag(4, b"", 0x10) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="footer"):
        _tag(mp3)


def test_an_id3_header_with_a_0xff_revision_is_an_id3_error(tmp_path: Path) -> None:
    """[if] an ID3v2 header's revision byte is 0xFF [then] Id3Error, not a clean empty tag, [else stop]."""
    mp3 = tmp_path / "revision_ff.mp3"
    mp3.write_bytes(b"ID3\x03\xff\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="revision byte is 0xFF"):
        id3v2.read_tag(mp3)


def _v23_crc_tag(tmp_path: Path, crc: int) -> Path:
    frames = b"TIT2" + struct.pack(">I", 2) + b"\x00\x00" + b"\x00A"  # a v2.3 frame, latin-1 text "A"
    ext = struct.pack(">I", 10) + b"\x80\x00" + struct.pack(">I", 0) + struct.pack(">I", crc)  # CRC flag set
    mp3 = tmp_path / f"v23_crc_{crc:08x}.mp3"
    mp3.write_bytes(_raw_tag(3, ext + frames, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    return mp3


def _syncsafe5(value: int) -> bytes:
    return bytes([(value >> 28) & 0x0F, (value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F])


def _v24_crc_tag(tmp_path: Path, crc: int) -> Path:
    frames = b"TIT2" + b"\x00\x00\x00\x02" + b"\x00\x00" + b"\x00A"  # a v2.4 frame, latin-1 text "A"
    ext = b"\x00\x00\x00\x0c" + b"\x01" + b"\x20" + b"\x05" + _syncsafe5(crc)  # CRC flag set
    mp3 = tmp_path / f"v24_crc_{crc:08x}.mp3"
    mp3.write_bytes(_raw_tag(4, ext + frames, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    return mp3


def test_a_v23_extended_header_crc_over_the_frames_is_verified(tmp_path: Path) -> None:
    """[if] a v2.3 extended header's CRC matches its frames [then] the tag reads, [else] Id3Error, [else stop]."""
    import zlib

    frames = b"TIT2" + struct.pack(">I", 2) + b"\x00\x00" + b"\x00A"
    good = zlib.crc32(frames)
    assert [f.frame_id for f in _tag(_v23_crc_tag(tmp_path, good)).frames] == ["TIT2"]
    with pytest.raises(id3v2.Id3Error, match="does not match the tag data"):
        id3v2.read_tag(_v23_crc_tag(tmp_path, good ^ 1))


def test_a_v24_extended_header_crc_over_the_frames_is_verified(tmp_path: Path) -> None:
    """[if] a v2.4 extended header's CRC matches the data after it [then] the tag reads, [else] Id3Error, [else stop]."""
    import zlib

    frames = b"TIT2" + b"\x00\x00\x00\x02" + b"\x00\x00" + b"\x00A"
    good = zlib.crc32(frames)
    assert [f.frame_id for f in _tag(_v24_crc_tag(tmp_path, good)).frames] == ["TIT2"]
    with pytest.raises(id3v2.Id3Error, match="does not match the tag data"):
        id3v2.read_tag(_v24_crc_tag(tmp_path, good ^ 1))


def test_a_v24_crc_is_not_checked_when_a_frame_is_unsynchronised(tmp_path: Path) -> None:
    """[if] a v2.4 frame is unsynchronised [then] its extended-header CRC is not checked, since its coverage is unpinned, [else stop]."""
    frames = b"TIT2" + b"\x00\x00\x00\x03" + b"\x00\x02" + b"\x00A\x00"  # unsynchronisation flag set
    ext = b"\x00\x00\x00\x0c" + b"\x01" + b"\x20" + b"\x05" + _syncsafe5(0x12345678)  # a CRC that matches nothing
    mp3 = tmp_path / "v24_unsync_crc.mp3"
    mp3.write_bytes(_raw_tag(4, ext + frames, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    assert [f.frame_id for f in _tag(mp3).frames] == ["TIT2"]


def test_a_v24_tag_with_a_footer_and_padding_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.4 tag sets the footer flag and has padding after its frames [then] Id3Error, since v2.4 forbids that, [else stop]."""
    frame = b"TIT2" + bytes([0, 0, 0, 2]) + b"\x00\x00" + b"\x00A"  # latin-1 text "A", syncsafe size 2
    body = frame + b"\x00\x00"  # two NUL bytes of padding
    header = b"ID3\x04\x00\x10" + bytes([0, 0, 0, len(body)])
    footer = b"3DI" + header[3:10]
    mp3 = tmp_path / "footer_and_padding.mp3"
    mp3.write_bytes(header + body + footer + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="has a footer but 2 padding bytes"):
        id3v2.read_tag(mp3)


def test_a_v24_footer_that_disagrees_with_the_header_is_an_error(tmp_path: Path) -> None:
    """[if] a v2.4 footer starts 3DI but its fields differ from the header [then] Id3Error, [else stop]."""
    footer = b"3DI\x04\x00\x10" + b"\x00\x00\x00\x07"  # header size is 0, footer declares 7
    mp3 = tmp_path / "bad-footer.mp3"
    mp3.write_bytes(_raw_tag(4, b"", 0x10) + footer + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="footer"):
        _tag(mp3)


def test_a_v23_padding_larger_than_the_tag_holds_is_an_error(tmp_path: Path) -> None:
    """[if] a v2.3 extended header declares more padding than the tag holds [then] Id3Error, [else stop]."""
    ext = struct.pack(">I", 6) + b"\x00\x00" + struct.pack(">I", 1)  # size 6, no flags, padding 1
    mp3 = tmp_path / "padding.mp3"
    mp3.write_bytes(_raw_tag(3, ext, 0x40) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="padding"):
        _tag(mp3)


@pytest.mark.parametrize(("version", "fmt"), [(3, 0x10), (3, 0x1F), (4, 0x80), (4, 0x20), (4, 0x10)])
def test_a_reserved_frame_format_flag_bit_is_an_error(tmp_path: Path, version: int, fmt: int) -> None:
    """[if] a frame sets a format-flag bit its version reserves [then] Id3Error, [else stop]."""
    frame = b"TIT2" + struct.pack(">I", 2) + bytes([0, fmt]) + b"\x00A"
    mp3 = tmp_path / "frame-flags.mp3"
    mp3.write_bytes(_raw_tag(version, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="reserved format flag bits"):
        _tag(mp3)


@pytest.mark.parametrize(("version", "status"), [(3, 0x01), (3, 0x1F), (4, 0x80), (4, 0x0F)])
def test_a_reserved_frame_status_flag_bit_is_an_error(tmp_path: Path, version: int, status: int) -> None:
    """[if] a frame sets a status-flag bit its version reserves [then] Id3Error, [else stop]."""
    frame = b"TIT2" + struct.pack(">I", 2) + bytes([status, 0]) + b"\x00A"
    mp3 = tmp_path / "frame-status.mp3"
    mp3.write_bytes(_raw_tag(version, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="reserved status flag bits"):
        _tag(mp3)


def test_v23_padding_that_understates_the_suffix_is_an_error(tmp_path: Path) -> None:
    """[if] a v2.3 padding size is less than the bytes after the frames [then] Id3Error, [else stop]."""
    ext = struct.pack(">I", 6) + b"\x00\x00" + struct.pack(">I", 1)  # declares 1 padding byte
    frame = b"TIT2" + struct.pack(">I", 2) + b"\x00\x00" + b"\x00A"
    mp3 = tmp_path / "understated.mp3"
    mp3.write_bytes(_raw_tag(3, ext + frame + b"\x00\x00", 0x40) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="padding"):
        _tag(mp3)


def test_nul_bytes_ending_a_frame_are_not_counted_as_padding(tmp_path: Path) -> None:
    """[if] a frame's payload ends in NUL bytes [then] they are not padding, so Id3Error, [else stop]."""
    ext = struct.pack(">I", 6) + b"\x00\x00" + struct.pack(">I", 5)  # v2.3 extended header declares 5 padding bytes
    frame = b"TIT2" + struct.pack(">I", 5) + b"\x00\x00" + b"\x00AB\x00\x00"
    mp3 = tmp_path / "nul-padding.mp3"
    mp3.write_bytes(_raw_tag(3, ext + frame + b"\x00\x00\x00", 0x40) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="padding"):
        _tag(mp3)


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


def _geob_body(description: bytes) -> bytes:
    return b"\x00" + b"application/octet-stream\x00" + b"\x00" + description + b"\x00" + b"payload"


def test_a_v24_grouped_frame_is_decoded_not_sealed(tmp_path: Path) -> None:
    """[if] a v2.4 frame carries the grouping flag (0x40, v2.3's encryption bit) [then] it is
    decoded past its group byte and written back with it, [else stop]."""
    body = b"\x07" + _geob_body(b"Serato Markers2")  # group id 7, then the GEOB body
    frame = b"GEOB" + bytes([0, 0, 0, len(body)]) + b"\x00\x40" + body
    mp3 = tmp_path / "g.mp3"
    mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
    tag = _tag(mp3)
    (geob,) = tag.frames
    assert not geob.opaque
    assert geob.group_id == 7
    assert [desc for _, _, desc, _ in tag.geob()] == ["Serato Markers2"]
    # written back with its group byte and flag intact
    tag.set_text("TIT2", "added")
    id3v2.save(mp3, tag)
    (kept,) = [f for f in _tag(mp3).frames if f.frame_id == "GEOB"]
    assert (kept.format_flags, kept.group_id, kept.data) == (0x40, 7, _geob_body(b"Serato Markers2"))


@pytest.mark.parametrize(("version", "flag"), [(4, 0x04), (4, 0x08), (3, 0x40), (3, 0x80)])
def test_a_sealed_frame_is_opaque_under_its_own_version(tmp_path: Path, version: int, flag: int) -> None:
    """[if] a frame is encrypted or compressed under its tag's version [then] it is opaque and not decoded, [else stop]."""
    body = b"\x80" + b"\x5a" * 20
    size = bytes([0, 0, 0, len(body)])
    mp3 = tmp_path / "s.mp3"
    mp3.write_bytes(_raw_tag(version, b"GEOB" + size + bytes([0, flag]) + body) + b"\xff\xfb\x90\x00" * 4)
    tag = _tag(mp3)
    assert tag.frames[0].opaque
    assert tag.geob() == []


def test_a_v23_grouped_frame_is_decoded_past_its_group_byte(tmp_path: Path) -> None:
    """[if] a v2.3 frame carries the grouping flag (0x20) [then] its group byte is held apart and the frame decodes, [else stop]."""
    body = b"\x03" + _geob_body(b"Serato Markers2")
    mp3 = tmp_path / "g3.mp3"
    mp3.write_bytes(_raw_tag(3, b"GEOB" + struct.pack(">I", len(body)) + b"\x00\x20" + body) + b"\xff\xfb\x90\x00" * 4)
    tag = _tag(mp3)
    assert tag.frames[0].group_id == 3
    assert [desc for _, _, desc, _ in tag.geob()] == ["Serato Markers2"]


def test_a_v24_grouped_unsynchronised_frame_undoes_unsync_before_its_group_byte(tmp_path: Path) -> None:
    """[if] a v2.4 frame is grouped and unsynchronised, with group id 0xff before a 0x00 [then] the
    inserted zero is removed before the group byte is read, so the frame decodes, [else stop]."""
    body = b"\xff" + _geob_body(b"Serato Markers2")  # group 0xff, then encoding byte 0x00
    stored = body.replace(b"\xff\x00", b"\xff\x00\x00")  # unsynchronised: ff 00 00
    assert stored != body  # control: the unsync scheme really inserted a byte here
    frame = b"GEOB" + bytes([0, 0, 0, len(stored)]) + b"\x00\x42" + stored
    mp3 = tmp_path / "gu.mp3"
    mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
    tag = _tag(mp3)
    assert tag.frames[0].group_id == 0xFF
    assert [desc for _, _, desc, _ in tag.geob()] == ["Serato Markers2"]


def test_a_v24_frame_without_unsync_keeps_its_ff_00_bytes(tmp_path: Path) -> None:
    """[if] a v2.4 grouped frame is NOT unsynchronised and its body holds ff 00 [then] those bytes
    are kept as stored, [else stop]."""
    payload = b"\xff\x00\xe0"
    body = b"\x05" + b"\x00" + b"application/octet-stream\x00" + b"\x00" + b"Serato Markers2\x00" + payload
    frame = b"GEOB" + bytes([0, 0, 0, len(body)]) + b"\x00\x40" + body
    mp3 = tmp_path / "nu.mp3"
    mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
    assert [p for _, _, _, p in _tag(mp3).geob()] == [payload]


def test_a_v23_extended_header_with_too_few_bytes_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.3 tag sets the extended-header flag but has fewer than four body bytes [then] Id3Error, not struct.error, [else stop]."""
    mp3 = tmp_path / "short_ext.mp3"
    mp3.write_bytes(_raw_tag(3, b"\x00\x00", flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="extended header is truncated"):
        id3v2.read_tag(mp3)


def test_a_tag_size_with_a_high_bit_is_an_id3_error_not_an_empty_tag(tmp_path: Path) -> None:
    """[if] an ID3 header's size bytes are not syncsafe [then] reading the tag raises Id3Error rather than reading an empty tag, [else stop]."""
    mp3 = tmp_path / "unsafe_size.mp3"
    mp3.write_bytes(b"ID3\x03\x00\x00\x80\x00\x00\x00" + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="not syncsafe"):
        id3v2.read_tag(mp3)


def test_a_v24_extended_header_size_with_a_high_bit_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.4 extended header's size bytes are not syncsafe [then] reading the tag raises Id3Error, not a masked size, [else stop]."""
    mp3 = tmp_path / "unsafe_ext.mp3"
    body = b"\x80\x00\x00\x06" + b"\x01\x00"  # non-syncsafe size, then a six-byte header
    mp3.write_bytes(_raw_tag(4, body, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="not syncsafe"):
        id3v2.read_tag(mp3)


def test_a_v24_extended_header_with_a_reserved_flag_bit_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.4 extended header sets a reserved flag bit [then] reading the tag raises Id3Error rather than reading an empty tag, [else stop]."""
    mp3 = tmp_path / "reserved_flag.mp3"
    body = b"\x00\x00\x00\x06" + b"\x01" + b"\x01"  # six-byte header, one flag byte set to the reserved 0x01
    mp3.write_bytes(_raw_tag(4, body, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="reserved flag bits 0x01"):
        id3v2.read_tag(mp3)


def test_a_v23_extended_header_with_a_reserved_flag_bit_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.3 extended header sets a reserved flag bit [then] reading the tag raises Id3Error rather than reading an empty tag, [else stop]."""
    mp3 = tmp_path / "reserved_v23.mp3"
    body = b"\x00\x00\x00\x06" + b"\x00\x01" + b"\x00\x00\x00\x00"  # six field bytes, reserved flag 0x0001 set
    mp3.write_bytes(_raw_tag(3, body, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="reserved flag bits"):
        id3v2.read_tag(mp3)


def test_a_v24_data_length_indicator_on_a_short_frame_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.4 frame sets the data-length-indicator flag but holds under four bytes [then] reading the tag raises Id3Error, [else stop]."""
    mp3 = tmp_path / "short_dli.mp3"
    frame = b"TIT2" + b"\x00\x00\x00\x01" + b"\x00\x01" + b"\x00"  # flag 0x0001 set, one data byte
    mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="data-length indicator"):
        id3v2.read_tag(mp3)


def test_a_v24_data_length_indicator_that_disagrees_with_the_frame_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.4 frame's data-length indicator declares other than the bytes it holds [then] reading the tag raises Id3Error, [else stop]."""
    mp3 = tmp_path / "wrong_dli.mp3"
    frame = b"TIT2" + b"\x00\x00\x00\x05" + b"\x00\x01" + b"\x00\x00\x00\x00" + b"A"  # declares 0 bytes, holds 1
    mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="data-length indicator declares 0 bytes"):
        id3v2.read_tag(mp3)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"\x00application/octet-stream",
        b"\x00application/octet-stream\x00Serato Markers2\x00" + b"payload",  # no filename terminator
        b"\x00application/octet-stream\x00\x00" + b"payload",  # no description terminator
    ],
)
def test_a_malformed_geob_body_is_an_id3_error(tmp_path: Path, body: bytes) -> None:
    """[if] a GEOB body is empty or lacks a MIME, filename or description terminator [then] decoding raises Id3Error, [else stop]."""
    frame = b"GEOB" + struct.pack(">I", len(body)) + b"\x00\x00" + body
    mp3 = tmp_path / "bad_geob.mp3"
    mp3.write_bytes(_raw_tag(3, frame) + b"\xff\xfb\x90\x00" * 4)
    # An empty body fails before any accessor decodes it: a zero size or a missing encoding byte.
    with pytest.raises(id3v2.Id3Error, match=r"malformed geob frame body|has no encoding byte|has no data bytes"):
        _tag(mp3).geob()


@pytest.mark.parametrize("tail", [b"G", b"GEOB\x00\x00\x00\x05\x00"])
def test_a_partial_frame_header_at_the_end_of_a_tag_is_an_id3_error(tmp_path: Path, tail: bytes) -> None:
    """[if] the tag body ends in fewer bytes than a frame header and they are not padding [then] Id3Error, not an empty tag, [else stop]."""
    mp3 = tmp_path / "partial_header.mp3"
    mp3.write_bytes(_raw_tag(3, tail) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="are not a whole frame header"):
        _tag(mp3).geob()


def test_non_padding_bytes_after_a_zero_byte_are_an_id3_error(tmp_path: Path) -> None:
    """[if] a tag body starts with a zero byte but holds non-zero bytes after it [then] Id3Error, not an empty tag, [else stop]."""
    mp3 = tmp_path / "zero_then_junk.mp3"
    mp3.write_bytes(_raw_tag(3, b"\x00NOTPADDING") + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="are not a whole frame header"):
        _tag(mp3).geob()


def test_zero_padding_after_the_last_frame_is_not_a_partial_header(tmp_path: Path) -> None:
    """[if] the tag body ends in zero padding shorter than a frame header [then] it reads as no frames, not an error, [else stop]."""
    mp3 = tmp_path / "short_padding.mp3"
    mp3.write_bytes(_raw_tag(3, b"\x00\x00\x00") + b"\xff\xfb\x90\x00" * 4)
    assert list(_tag(mp3).geob()) == []


def test_a_malformed_geob_is_a_counted_loss_for_strict_and_empty_for_lenient(tmp_path: Path) -> None:
    """[if] a GEOB body is malformed [then] strict reads raise Id3Error and lenient reads return an empty bundle, [else stop]."""
    from apps.adapters.serato.geob import SeratoGEOB, read_geob_frames

    frame = b"GEOB" + struct.pack(">I", 0) + b"\x00\x00"
    mp3 = tmp_path / "bad_geob_serato.mp3"
    mp3.write_bytes(_raw_tag(3, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error):
        read_geob_frames(mp3, strict=True)
    assert read_geob_frames(mp3, strict=False) == SeratoGEOB()


@pytest.mark.parametrize(
    ("version", "body", "match"),
    [
        (4, b"\x00\x00\x00\x06" + b"\x00\x00\x00\x00\x00\x00", "has 0 flag bytes"),
        (3, b"\x00\x00\x00\x07" + b"\x00\x00\x00\x00\x00\x00\x00", "has 6 or 10"),
        (3, b"\x00\x00\x00\x06" + b"\x80\x00\x00\x00\x00\x00", "sets the CRC flag"),
        (3, b"\x00\x00\x00\x0a" + b"\x00" * 10, "does not set the CRC flag"),
    ],
)
def test_an_extended_header_with_an_invalid_field_is_an_id3_error(tmp_path: Path, version: int, body: bytes, match: str) -> None:
    """[if] an extended header's fixed fields are invalid for its version [then] Id3Error, not an empty tag, [else stop]."""
    mp3 = tmp_path / f"ext_field_v{version}.mp3"
    mp3.write_bytes(_raw_tag(version, body, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match=match):
        id3v2.read_tag(mp3)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (b"\x00\x00\x00\x06\x01\x20", "flag 0x20 has no valid 5-byte payload"),
        (b"\x00\x00\x00\x06\x01\x40\x01", "flag 0x40 has no valid 0-byte payload"),
        (b"\x00\x00\x00\x07\x01\x10\x01", "flag 0x10 has no valid 1-byte payload"),
        (b"\x00\x00\x00\x07\x01\x00\x01", "flags account for 6"),
    ],
)
def test_a_v24_extended_header_with_a_truncated_flag_payload_is_an_id3_error(tmp_path: Path, body: bytes, match: str) -> None:
    """[if] a v2.4 extended header sets a flag whose length byte or payload is missing or wrong [then] Id3Error, not an empty tag, [else stop]."""
    mp3 = tmp_path / "ext_flag_payload.mp3"
    mp3.write_bytes(_raw_tag(4, body, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match=match):
        id3v2.read_tag(mp3)


def test_a_v24_extended_header_with_a_complete_crc_payload_is_accepted(tmp_path: Path) -> None:
    """[if] a v2.4 extended header sets the CRC flag and carries its five-byte payload [then] it is read as a valid header, [else stop]."""
    body = b"\x00\x00\x00\x0c\x01\x20\x05" + b"\x00\x00\x00\x00\x00"
    mp3 = tmp_path / "ext_crc_ok.mp3"
    mp3.write_bytes(_raw_tag(4, body, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    assert list(_tag(mp3).geob()) == []


def test_a_v24_extended_header_crc_payload_with_a_high_bit_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.4 extended header's CRC payload is not five syncsafe bytes [then] Id3Error, not a clean tag, [else stop]."""
    body = b"\x00\x00\x00\x0c\x01\x20\x05" + b"\x80\x00\x00\x00\x00"
    mp3 = tmp_path / "ext_crc_not_syncsafe.mp3"
    mp3.write_bytes(_raw_tag(4, body, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="CRC payload is not syncsafe"):
        id3v2.read_tag(mp3)


def test_a_v24_extended_header_crc_payload_wider_than_32_bits_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.4 extended header's CRC payload needs more than 32 bits [then] Id3Error, not a clean tag, [else stop]."""
    body = b"\x00\x00\x00\x0c\x01\x20\x05" + b"\x10\x00\x00\x00\x00"  # the top byte's 0x10 is 2**32, one bit past a CRC-32
    mp3 = tmp_path / "ext_crc_too_wide.mp3"
    mp3.write_bytes(_raw_tag(4, body, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="wider than a 32-bit CRC"):
        id3v2.read_tag(mp3)


@pytest.mark.parametrize(("version", "declared"), [(3, 0), (3, 1), (3, 5), (4, 0), (4, 1), (4, 5)])
def test_an_extended_header_too_small_for_its_version_is_an_id3_error(tmp_path: Path, version: int, declared: int) -> None:
    """[if] an extended header declares fewer bytes than its version's minimum [then] Id3Error, not an empty tag, [else stop]."""
    size = struct.pack(">I", declared) if version == 3 else bytes([0, 0, 0, declared])
    body = size + b"\x00" * 6
    mp3 = tmp_path / f"ext_too_small_v{version}_{declared}.mp3"
    mp3.write_bytes(_raw_tag(version, body, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="fewer than the"):
        id3v2.read_tag(mp3)


@pytest.mark.parametrize(("version", "declared"), [(3, 100), (4, 100)])
def test_an_extended_header_that_runs_past_the_tag_is_an_id3_error(tmp_path: Path, version: int, declared: int) -> None:
    """[if] an extended header declares more bytes than the tag body holds [then] Id3Error, not an empty tag, [else stop]."""
    size = struct.pack(">I", declared) if version == 3 else bytes([0, 0, 0, declared])
    body = size + b"\x00" * 6
    mp3 = tmp_path / f"ext_past_end_v{version}.mp3"
    mp3.write_bytes(_raw_tag(version, body, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="extended header declares"):
        id3v2.read_tag(mp3)


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_v23_frame_with_a_v24_only_text_encoding_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.3 GEOB uses text encoding 3, which only v2.4 defines [then] reading it raises Id3Error, [else stop]."""
    body = b"\x03" + b"application/octet-stream\x00" + b"f\x00" + b"\x00" + b"payload"
    with pytest.raises(id3v2.Id3Error, match=r"only v2\.4 defines"):
        id3v2._decode_geob(body, 3)
    frame = b"GEOB" + struct.pack(">I", len(body)) + b"\x00\x00" + body
    mp3 = tmp_path / "v23_utf8_geob.mp3"
    mp3.write_bytes(_raw_tag(3, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match=r"only v2\.4 defines"):
        _tag(mp3).geob()


def test_a_v24_frame_with_the_same_text_encoding_still_decodes(tmp_path: Path) -> None:
    """[if] a v2.4 GEOB uses text encoding 3 [then] it decodes, and an in-memory frame (major 0) is not checked, [else stop]."""
    body = b"\x03" + b"application/octet-stream\x00" + b"f\x00" + b"\x00" + b"payload"
    assert id3v2._decode_geob(body, 4)[1] == "f"
    assert id3v2._decode_geob(body)[1] == "f"
    frame = b"GEOB" + bytes([0, 0, 0, len(body)]) + b"\x00\x00" + body  # the size is syncsafe, and small enough to need no high bits
    mp3 = tmp_path / "v24_utf8_geob.mp3"
    mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
    assert [g[1] for g in _tag(mp3).geob()] == ["f"]


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_v23_text_frame_no_accessor_reads_is_still_checked_when_the_tag_is_read(tmp_path: Path) -> None:
    """[if] a v2.3 TIT2 frame uses text encoding 3 and no accessor is asked for it [then] reading the tag raises Id3Error, [else stop]."""
    frame = b"TIT2" + struct.pack(">I", 4) + b"\x00\x00" + b"\x03bad"
    mp3 = tmp_path / "v23_bad_tit2.mp3"
    mp3.write_bytes(_raw_tag(3, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match=r"only v2\.4 defines"):
        id3v2.read_tag(mp3)


def test_a_v24_text_frame_with_encoding_3_is_not_checked_at_read(tmp_path: Path) -> None:
    """[if] a v2.4 TIT2 frame uses text encoding 3 [then] reading the tag succeeds, [else stop]."""
    frame = b"TIT2" + bytes([0, 0, 0, 4]) + b"\x00\x00" + b"\x03bad"
    mp3 = tmp_path / "v24_utf8_tit2.mp3"
    mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
    assert [f.frame_id for f in _tag(mp3).frames] == ["TIT2"]


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_v24_text_frame_with_an_undefined_encoding_byte_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.4 TIT2 frame uses an encoding byte the spec does not define [then] reading the tag raises Id3Error, [else stop]."""
    frame = b"TIT2" + bytes([0, 0, 0, 2]) + b"\x00\x00" + b"\xffA"
    mp3 = tmp_path / "v24_bad_encoding.mp3"
    mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="does not define"):
        id3v2.read_tag(mp3)


@pytest.mark.requirement("DJIMPORT-6039")
@pytest.mark.parametrize("version", [3, 4])
def test_an_empty_text_frame_has_no_encoding_byte_and_is_an_id3_error(tmp_path: Path, version: int) -> None:
    """[if] a text-bearing frame has no body bytes [then] reading the tag raises Id3Error for lack of an encoding byte, [else stop]."""
    frame = b"TIT2" + bytes([0, 0, 0, 0]) + b"\x00\x00"
    mp3 = tmp_path / f"empty_text_v2{version}.mp3"
    mp3.write_bytes(_raw_tag(version, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match=r"has no encoding byte|has no data bytes"):
        id3v2.read_tag(mp3)


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_text_frame_with_bytes_its_encoding_cannot_decode_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.4 TIT2 declares UTF-8 but holds bytes it cannot decode [then] reading the tag raises Id3Error, not a replacement, [else stop]."""
    frame = b"TIT2" + bytes([0, 0, 0, 2]) + b"\x00\x00" + b"\x03\xff"
    mp3 = tmp_path / "v24_bad_utf8.mp3"
    mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="not valid for its encoding"):
        id3v2.read_tag(mp3)


def test_latin1_text_with_high_bytes_is_valid_and_reads(tmp_path: Path) -> None:
    """[if] a TIT2 frame declares ISO-8859-1 and holds high bytes, which every byte decodes as [then] it reads, [else stop]."""
    frame = b"TIT2" + bytes([0, 0, 0, 2]) + b"\x00\x00" + b"\x00\xff"
    mp3 = tmp_path / "v24_latin1.mp3"
    mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
    assert _tag(mp3).first_text("TIT2") == "ÿ"


@pytest.mark.requirement("DJIMPORT-6039")
def test_utf16_text_without_a_byte_order_mark_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.3 TIT2 declares UTF-16 but its text has no byte-order mark [then] reading the tag raises Id3Error, [else stop]."""
    frame = b"TIT2" + struct.pack(">I", 3) + b"\x00\x00" + b"\x01A\x00"
    mp3 = tmp_path / "v23_utf16_no_bom.mp3"
    mp3.write_bytes(_raw_tag(3, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="not valid for its encoding"):
        id3v2.read_tag(mp3)


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_utf16_text_whose_later_byte_order_mark_conflicts_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.3 TIT2 starts little-endian and a later value carries a big-endian BOM [then] reading the tag raises
    Id3Error, because every string in a frame shares one byte order, [else stop]."""
    frame = b"TIT2" + struct.pack(">I", 11) + b"\x00\x00" + b"\x01\xff\xfeA\x00\x00\x00\xfe\xff\x00B"
    mp3 = tmp_path / "v23_utf16_conflicting_bom.mp3"
    mp3.write_bytes(_raw_tag(3, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="not valid for its encoding"):
        id3v2.read_tag(mp3)


@pytest.mark.requirement("DJIMPORT-6039")
def test_an_id3_signature_with_a_cut_short_header_is_an_id3_error_not_no_tag(tmp_path: Path) -> None:
    """[if] a file starts with ID3 but ends before its 10-byte header is complete [then] reading raises Id3Error,
    not None, so a strict read counts it [else stop]."""
    short = tmp_path / "cut_short.mp3"
    short.write_bytes(b"ID3\x03\x00")
    with pytest.raises(id3v2.Id3Error, match="cut short"):
        id3v2.read_tag(short)
    untagged = tmp_path / "no_tag.mp3"
    untagged.write_bytes(b"\xff\xfb\x90\x00" * 4)
    assert id3v2.read_tag(untagged) is None  # control: a file with no ID3 signature has no tag


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_utf16be_text_frame_that_starts_with_a_byte_order_mark_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.4 TIT2 declares encoding 2 (UTF-16BE, no BOM) but starts with FE FF [then] reading the tag raises
    Id3Error, and the same text without the BOM reads cleanly, [else stop]."""
    control = b"TIT2" + b"\x00\x00\x00\x03" + b"\x00\x00" + b"\x02\x00A"
    ok = tmp_path / "v24_utf16be_clean.mp3"
    ok.write_bytes(_raw_tag(4, control) + b"\xff\xfb\x90\x00" * 4)
    assert id3v2.read_tag(ok) is not None  # control: no BOM reads
    for bom in (b"\xfe\xff", b"\xff\xfe"):  # either byte order of the mark is malformed in encoding 2
        frame = b"TIT2" + b"\x00\x00\x00\x05" + b"\x00\x00" + b"\x02" + bom + b"\x00A"
        mp3 = tmp_path / f"v24_utf16be_bom_{bom.hex()}.mp3"
        mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
        with pytest.raises(id3v2.Id3Error):
            id3v2.read_tag(mp3)


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_byte_order_mark_in_a_later_utf16be_value_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.4 encoding-2 TIT2 holds a second NUL-separated value that starts with a byte-order mark [then] reading
    raises Id3Error, and the same two values without the mark read cleanly, [else stop]."""
    control = b"TIT2" + b"\x00\x00\x00\x07" + b"\x00\x00" + b"\x02\x00A\x00\x00\x00B"
    ok = tmp_path / "v24_utf16be_two_values.mp3"
    ok.write_bytes(_raw_tag(4, control) + b"\xff\xfb\x90\x00" * 4)
    assert id3v2.read_tag(ok) is not None  # control: two values without a mark read
    frame = b"TIT2" + b"\x00\x00\x00\x09" + b"\x00\x00" + b"\x02\x00A\x00\x00\xfe\xff\x00B"
    mp3 = tmp_path / "v24_utf16be_later_bom.mp3"
    mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error):
        id3v2.read_tag(mp3)


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_txxx_whose_value_has_a_conflicting_byte_order_mark_is_an_id3_error() -> None:
    """[if] a TXXX description is little-endian and its value carries a big-endian BOM [then] the strict decode raises,
    because every string in a frame shares one byte order, [else stop]."""
    data = b"\x01" + b"\xff\xfeA\x00" + b"\x00\x00" + b"\xfe\xff\x00B"
    with pytest.raises(id3v2.Id3Error):
        id3v2._decode_txxx(data, 3, "strict")


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_txxx_value_without_a_byte_order_mark_uses_the_byte_order_its_frame_set() -> None:
    """[if] a TXXX description sets a little-endian byte order and its value has no BOM [then] the value decodes in that
    order, since the frame's strings share one byte order, [else stop]."""
    data = b"\x01" + b"\xff\xfeA\x00" + b"\x00\x00" + b"B\x00"
    assert id3v2._decode_txxx(data, 3, "strict") == ("A", ["B"])


def test_utf16_text_with_a_byte_order_mark_reads(tmp_path: Path) -> None:
    """[if] a v2.3 TIT2 declares UTF-16 and its text starts with a little-endian BOM [then] it reads, [else stop]."""
    frame = b"TIT2" + struct.pack(">I", 5) + b"\x00\x00" + b"\x01\xff\xfeA\x00"
    mp3 = tmp_path / "v23_utf16_bom.mp3"
    mp3.write_bytes(_raw_tag(3, frame) + b"\xff\xfb\x90\x00" * 4)
    assert _tag(mp3).first_text("TIT2") == "A"


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_txxx_without_a_description_terminator_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.3 TXXX has no terminator after its description [then] reading the tag raises Id3Error, [else stop]."""
    frame = b"TXXX" + struct.pack(">I", 5) + b"\x00\x00" + b"\x00desc"
    mp3 = tmp_path / "v23_txxx_no_terminator.mp3"
    mp3.write_bytes(_raw_tag(3, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="malformed txxx frame body"):
        id3v2.read_tag(mp3)


@pytest.mark.requirement("DJIMPORT-6039")
@pytest.mark.parametrize("version", [3, 4])
def test_a_zero_length_frame_is_an_id3_error(tmp_path: Path, version: int) -> None:
    """[if] a v2.3 or v2.4 frame declares no data bytes [then] reading the tag raises Id3Error, since the spec requires one, [else stop]."""
    frame = b"XABC" + bytes([0, 0, 0, 0]) + b"\x00\x00"
    mp3 = tmp_path / f"zero_length_v2{version}.mp3"
    mp3.write_bytes(_raw_tag(version, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="has no data bytes"):
        id3v2.read_tag(mp3)


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_comm_frame_whose_language_is_not_an_iso_639_2_code_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a COMM frame's three-byte language field is not three ASCII letters [then] a strict read raises Id3Error,
    and a three-letter code, including XXX for unknown, reads cleanly, [else stop]."""
    control = b"COMM" + b"\x00\x00\x00\x09" + b"\x00\x00" + b"\x00eng\x00text"
    ok = tmp_path / "v24_comm_clean.mp3"
    ok.write_bytes(_raw_tag(4, control) + b"\xff\xfb\x90\x00" * 4)
    assert id3v2.read_tag(ok) is not None  # control: a three-letter language reads
    for language in (b"en\x00", b"e1n", b"\xff\xfe\xfd"):
        frame = b"COMM" + b"\x00\x00\x00\x09" + b"\x00\x00" + b"\x00" + language + b"\x00text"
        mp3 = tmp_path / f"v24_comm_bad_{language.hex()}.mp3"
        mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
        with pytest.raises(id3v2.Id3Error):
            id3v2.read_tag(mp3)


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_v24_footer_is_read_after_the_declared_size_and_is_not_frame_data(tmp_path: Path) -> None:
    """[if] a v2.4 tag sets the footer flag and its declared size excludes the 10-byte footer [then] the frames read
    cleanly, the footer is not parsed as audio or frames, and the tag's original size counts header, body and footer,
    [else stop]."""
    frame = b"TIT2" + bytes([0, 0, 0, 2]) + b"\x00\x00" + b"\x00A"  # latin-1 text "A", syncsafe size 2
    header = b"ID3\x04\x00\x10" + bytes([0, 0, 0, len(frame)])  # declared size is the frames only
    footer = b"3DI" + header[3:10]
    mp3 = tmp_path / "v24_footer_clean.mp3"
    mp3.write_bytes(header + frame + footer + b"\xff\xfb\x90\x00" * 4)
    tag = _tag(mp3)
    assert [f.frame_id for f in tag.frames] == ["TIT2"]
    assert tag.original_size == 10 + len(frame) + 10


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_popm_frame_without_its_email_terminator_or_rating_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a POPM frame lacks the NUL after its email or the rating byte after that NUL [then] a strict read raises
    Id3Error, and a frame with both reads cleanly, [else stop]."""
    control = b"POPM" + b"\x00\x00\x00\x07" + b"\x00\x00" + b"a@b.c\x00\x80"
    ok = tmp_path / "v24_popm_clean.mp3"
    ok.write_bytes(_raw_tag(4, control) + b"\xff\xfb\x90\x00" * 4)
    assert id3v2.read_tag(ok) is not None  # control: an email, NUL and rating read
    for body in (b"abc", b"a@b.c\x00"):
        frame = b"POPM" + bytes([0, 0, 0, len(body)]) + b"\x00\x00" + body
        mp3 = tmp_path / f"v24_popm_bad_{body.hex()}.mp3"
        mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
        with pytest.raises(id3v2.Id3Error):
            id3v2.read_tag(mp3)


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_utf16_field_with_no_byte_order_mark_is_an_id3_error_even_when_it_is_empty(tmp_path: Path) -> None:
    """[if] a v2.3 TXXX has an empty description with no byte-order mark, then a value that has one [then] a strict
    read raises Id3Error, and the same description with its own mark reads cleanly, [else stop]."""
    control = b"TXXX" + b"\x00\x00\x00\x09" + b"\x00\x00" + b"\x01\xff\xfe\x00\x00\xff\xfeA\x00"
    ok = tmp_path / "v23_txxx_clean.mp3"
    ok.write_bytes(_raw_tag(3, control) + b"\xff\xfb\x90\x00" * 4)
    assert id3v2.read_tag(ok) is not None  # control: a described field with its own mark reads
    frame = b"TXXX" + b"\x00\x00\x00\x07" + b"\x00\x00" + b"\x01\x00\x00\xff\xfeA\x00"
    mp3 = tmp_path / "v23_txxx_empty_no_bom.mp3"
    mp3.write_bytes(_raw_tag(3, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error):
        id3v2.read_tag(mp3)


@pytest.mark.requirement("DJIMPORT-6039")
def test_an_apic_picture_type_outside_the_defined_range_is_an_id3_error(tmp_path: Path) -> None:
    """[if] a v2.4 APIC has a picture type above 0x14 [then] a strict read raises Id3Error, and type 0x03 reads
    cleanly, [else stop]."""
    body = b"\x00" + b"image/png\x00" + b"{ptype}" + b"\x00" + b"img"
    control = b"APIC" + b"\x00\x00\x00\x10" + b"\x00\x00" + body.replace(b"{ptype}", b"\x03")
    ok = tmp_path / "v24_apic_clean.mp3"
    ok.write_bytes(_raw_tag(4, control) + b"\xff\xfb\x90\x00" * 4)
    assert id3v2.read_tag(ok) is not None  # control: picture type 0x03 reads
    frame = b"APIC" + b"\x00\x00\x00\x10" + b"\x00\x00" + body.replace(b"{ptype}", b"\xff")
    mp3 = tmp_path / "v24_apic_bad_type.mp3"
    mp3.write_bytes(_raw_tag(4, frame) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error):
        id3v2.read_tag(mp3)
