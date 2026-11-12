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
from pathlib import Path

import pytest

from apps.shared import file_rewrite, id3v2, tag_reader
from tests.fixtures import tagged_audio as ta
from tests.shared.id3v2_helpers import (
    _decodes,
    _geob_body,
    _raw_tag,
    _syncsafe5,
    _tag,
    _v23_crc_tag,
    _v24_crc_tag,
)

pytestmark = [pytest.mark.requirement("TAGIO-02"), pytest.mark.requires_ffmpeg]


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
        raw = raw[_tag(mp3).original_size :]
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


def test_a_failed_write_leaves_the_original_file_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
def test_an_extended_header_with_an_invalid_field_is_an_id3_error(
    tmp_path: Path, version: int, body: bytes, match: str
) -> None:
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
def test_a_v24_extended_header_with_a_truncated_flag_payload_is_an_id3_error(
    tmp_path: Path, body: bytes, match: str
) -> None:
    """[if] a v2.4 extended header sets a flag whose length byte or payload is missing or wrong [then] Id3Error, not an empty tag, [else stop]."""
    mp3 = tmp_path / "ext_flag_payload.mp3"
    mp3.write_bytes(_raw_tag(4, body, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match=match):
        id3v2.read_tag(mp3)
