"""TAGIO-05: the in-house Ogg Vorbis / Opus comment writer (apps.shared.ogg_comment).

[if] an Ogg comment is written [then] tinytag and ffprobe see it, audio identical, [else stop].

Base files are real Ogg Vorbis and Ogg Opus streams encoded and tagged by
ffmpeg. Every write is read back by tinytag and ffprobe; the pages are then
walked by a parser written here, with its own table-driven CRC, so the
writer's paging is never checked only by its own reader.

Regression one-liners:
  - if a written comment is not seen by tinytag and ffprobe then broken
  - if a comment that adds header pages changes the decoded audio then broken
  - if any page has a bad CRC or a sequence gap after a write then broken
  - if an audio page's payload or granule position changes then broken
  - if a failed write leaves the original changed or a temp file behind then broken
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from apps.shared import file_rewrite, ogg_comment, tag_reader
from tests.fixtures import tagged_audio as ta

pytestmark = [pytest.mark.requirement("TAGIO-05"), pytest.mark.requires_ffmpeg]

CODECS = ("ogg", "opus")


def _crc_table() -> list[int]:
    table = []
    for byte in range(256):
        crc = byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
        table.append(crc & 0xFFFFFFFF)
    return table


_TABLE = _crc_table()


def _reference_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _TABLE[(crc >> 24) ^ byte]
    return crc


def _pages(path: Path) -> list[tuple[int, int, int, bytes]]:
    """``(sequence, granule, header_type, body)`` per page; asserts every CRC."""
    raw = path.read_bytes()
    out = []
    pos = 0
    while pos < len(raw):
        assert raw[pos : pos + 4] == b"OggS", f"no page at {pos}"
        header_type, granule, _serial, sequence, crc, nsegs = struct.unpack_from("<BqIIIB", raw, pos + 5)
        lacing = raw[pos + 27 : pos + 27 + nsegs]
        end = pos + 27 + nsegs + sum(lacing)
        page = bytearray(raw[pos:end])
        page[22:26] = b"\x00\x00\x00\x00"
        assert _reference_crc(bytes(page)) == crc, f"bad CRC on page {sequence}"
        out.append((sequence, granule, header_type, raw[pos + 27 + nsegs : end]))
        pos = end
    return out


def test_the_crc_matches_an_independent_implementation() -> None:
    """[if] the zlib-based Ogg CRC is computed [then] it equals the table-driven one, [else stop]."""
    for data in (b"", b"OggS", bytes(range(256)) * 9):
        assert ogg_comment.ogg_crc(data) == _reference_crc(data)


@pytest.mark.parametrize("codec", CODECS)
def test_written_comments_are_seen_by_two_independent_readers(tmp_path: Path, codec: str) -> None:
    """[if] BPM / key / ISRC / rating / OPENDJ / comment (DESCRIPTION) are set [then] tinytag and ffprobe read them, [else stop]."""
    ogg = ta.make_tagged_audio(tmp_path, codec)
    meta = ogg_comment.read(ogg)
    for key, value in (("BPM", "128"), ("initialkey", "8A"), ("ISRC", "USRC17607839"), ("RATING", "4"),
                       ("OPENDJ_ENERGY", "7"), ("DESCRIPTION", "cue at 1:04 ☾")):
        meta.set(key, value)
    ogg_comment.save(ogg, meta)

    read = tag_reader.read_tags(ogg)
    assert (read.bpm, read.key, read.isrc, read.comment) == (128.0, "8A", "USRC17607839", "cue at 1:04 ☾")
    assert read.title == ta.STANDARD_TAGS["title"]  # untouched comment survives
    probed = ta.ffprobe_tags(ogg)
    assert (probed["bpm"], probed["initialkey"], probed["isrc"], probed["rating"], probed["opendj_energy"]) == (
        "128", "8A", "USRC17607839", "4", "7"
    )
    assert ogg_comment.read(ogg).get("INITIALKEY") == ["8A"]  # replaced, not duplicated


@pytest.mark.parametrize("codec", CODECS)
def test_a_comment_that_adds_pages_leaves_the_audio_identical(tmp_path: Path, codec: str) -> None:
    """[if] the comment spans extra pages [then] audio pages, granules and PCM are identical, [else stop]."""
    ogg = ta.make_tagged_audio(tmp_path, codec)
    before_meta = ogg_comment.read(ogg)
    pcm = ta.decoded_audio_sha256(ogg)
    audio_before = [(granule, body) for _s, granule, _t, body in _pages(ogg)[1 + before_meta.header_pages :]]

    meta = ogg_comment.read(ogg)
    meta.set("SERATO_BLOB", "y" * 200_000)  # several 64 KiB pages
    ogg_comment.save(ogg, meta)

    after_meta = ogg_comment.read(ogg)
    assert after_meta.header_pages > before_meta.header_pages
    pages = _pages(ogg)
    assert [seq for seq, _g, _t, _b in pages] == list(range(len(pages)))  # contiguous, renumbered
    assert [(g, b) for _s, g, _t, b in pages[1 + after_meta.header_pages :]] == audio_before
    assert ta.decoded_audio_sha256(ogg) == pcm
    assert tag_reader.read_tags(ogg).first_other("serato_blob") == "y" * 200_000


def test_a_write_that_keeps_the_page_count_copies_audio_pages_verbatim(tmp_path: Path) -> None:
    """[if] the header page count is unchanged [then] every byte after the headers is identical, [else stop]."""
    ogg = ta.make_tagged_audio(tmp_path, "ogg")
    meta = ogg_comment.read(ogg)
    tail = ogg.read_bytes()[meta.audio_offset :]
    meta.set("BPM", "99")
    ogg_comment.save(ogg, meta)
    after = ogg_comment.read(ogg)
    assert after.header_pages == meta.header_pages
    assert ogg.read_bytes()[after.audio_offset :] == tail


def test_the_vorbis_setup_header_is_kept_byte_for_byte(tmp_path: Path) -> None:
    """[if] a Vorbis comment is rewritten [then] the setup header packet is identical, [else stop]."""
    ogg = ta.make_tagged_audio(tmp_path, "ogg")
    before = ogg_comment.read(ogg)
    meta = ogg_comment.read(ogg)
    meta.set("SERATO_BLOB", "y" * 100_000)
    ogg_comment.save(ogg, meta)
    after = ogg_comment.read(ogg)
    assert after.setup_packets == before.setup_packets
    assert after.id_page == before.id_page


def test_a_failed_write_leaves_the_original_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] writing fails midway [then] the original file is byte-identical, [else stop]."""
    ogg = ta.make_tagged_audio(tmp_path, "opus")
    before = ogg.read_bytes()
    real_fsync = file_rewrite.os.fsync

    def failing_fsync(fd: int) -> None:
        real_fsync(fd)
        raise OSError("disk full")

    monkeypatch.setattr(file_rewrite.os, "fsync", failing_fsync)
    meta = ogg_comment.read(ogg)
    meta.set("SERATO_BLOB", "y" * 200_000)
    with pytest.raises(OSError, match="disk full"):
        ogg_comment.save(ogg, meta)
    assert ogg.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [ogg.name]


def test_a_non_vorbis_ogg_stream_is_refused(tmp_path: Path) -> None:
    """[if] the Ogg stream is FLAC-in-Ogg [then] OggError, nothing written, [else stop]."""
    flac = tmp_path / "flac-in-ogg.ogg"
    ta._ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "flac", "-f", "ogg", str(flac)])
    before = flac.read_bytes()
    with pytest.raises(ogg_comment.OggError, match="neither Vorbis nor Opus"):
        ogg_comment.read(flac)
    assert flac.read_bytes() == before


def test_a_corrupt_header_page_is_refused(tmp_path: Path) -> None:
    """[if] a header page's CRC does not match [then] OggError, [else stop]."""
    ogg = ta.make_tagged_audio(tmp_path, "ogg")
    raw = bytearray(ogg.read_bytes())
    raw[40] ^= 0xFF  # inside the identification header
    ogg.write_bytes(bytes(raw))
    with pytest.raises(ogg_comment.OggError, match="CRC mismatch"):
        ogg_comment.read(ogg)
