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

from apps.shared import id3v2
from tests.shared.id3v2_helpers import (
    _raw_tag,
    _tag,
)

pytestmark = [pytest.mark.requirement("TAGIO-02"), pytest.mark.requires_ffmpeg]


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
    body = (
        b"\x00\x00\x00\x0c\x01\x20\x05" + b"\x10\x00\x00\x00\x00"
    )  # the top byte's 0x10 is 2**32, one bit past a CRC-32
    mp3 = tmp_path / "ext_crc_too_wide.mp3"
    mp3.write_bytes(_raw_tag(4, body, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    with pytest.raises(id3v2.Id3Error, match="wider than a 32-bit CRC"):
        id3v2.read_tag(mp3)


@pytest.mark.parametrize(("version", "declared"), [(3, 0), (3, 1), (3, 5), (4, 0), (4, 1), (4, 5)])
def test_an_extended_header_too_small_for_its_version_is_an_id3_error(
    tmp_path: Path, version: int, declared: int
) -> None:
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
    frame = (
        b"GEOB" + bytes([0, 0, 0, len(body)]) + b"\x00\x00" + body
    )  # the size is syncsafe, and small enough to need no high bits
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


@pytest.mark.requirement("DJIMPORT-10062")
def test_read_tag_at_offset_reads_a_tag_after_leading_bytes(tmp_path: Path) -> None:
    """[if] an ID3 tag is not at file offset zero [then] read_tag(offset=) still reads it, [else stop]."""
    from tests.fixtures import serato_containers as sc

    inner = tmp_path / "inner.mp3"
    sc.write_mp3_with_serato(inner, b"\x01\x01")
    expected = id3v2.read_tag(inner)
    assert expected is not None
    junk = b"JUNK" * 4
    wrapped = tmp_path / "offset_tag.mp3"
    wrapped.write_bytes(junk + inner.read_bytes())
    tag = id3v2.read_tag(wrapped, offset=len(junk))
    assert tag is not None
    assert len(tag.geob()) == len(expected.geob())


@pytest.mark.requirement("DJIMPORT-10062")
def test_read_aiff_tag_finds_id3_chunk_or_raises_when_truncated(tmp_path: Path) -> None:
    """[if] an AIFF has no ID3 chunk [then] read_aiff_tag is None; truncated FORM raises, [else stop]."""
    from tests.fixtures import serato_containers as sc

    no_tag = tmp_path / "Invented No ID3.aiff"
    sc.write_aiff_no_id3(no_tag)
    assert id3v2.read_aiff_tag(no_tag) is None
    tagged = tmp_path / "Invented Tagged.aiff"
    sc.write_aiff_with_id3(tagged, sc.markers2_payload())
    assert id3v2.read_aiff_tag(tagged) is not None
    truncated = tmp_path / "Invented Truncated.aiff"
    chunk = b"COMM" + struct.pack(">I", 100)
    truncated.write_bytes(struct.pack(">4sI4s", b"FORM", 4 + len(chunk), b"AIFF") + chunk)
    with pytest.raises(id3v2.Id3Error, match=r"runs past"):
        id3v2.read_aiff_tag(truncated)


@pytest.mark.requirement("DJIMPORT-10062")
def test_read_aiff_tag_spec_chunk_order_byte_by_byte(tmp_path: Path) -> None:
    """[if] AIFF chunks are ckID then ckSize [then] read_aiff_tag finds ID3 at spec offsets, [else stop]."""
    empty_frames = b""
    syncsafe = bytes(((len(empty_frames) >> shift) & 0x7F) for shift in (21, 14, 7, 0))
    id3_body = b"ID3\x03\x00\x00" + syncsafe + empty_frames
    id3_chunk = b"ID3 " + struct.pack(">I", len(id3_body)) + id3_body
    comm_payload = b"\x00" * 18
    comm_chunk = b"COMM" + struct.pack(">I", len(comm_payload)) + comm_payload
    aiff_data = comm_chunk + id3_chunk
    form_size = 4 + len(aiff_data)
    path = tmp_path / "Invented Spec Order.aiff"
    path.write_bytes(b"FORM" + struct.pack(">I", form_size) + b"AIFF" + aiff_data)
    tag = id3v2.read_aiff_tag(path)
    assert tag is not None
    assert tag.frames == []
