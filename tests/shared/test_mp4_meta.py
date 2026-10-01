"""TAGIO-05: the in-house MP4 / M4A iTunes metadata writer (apps.shared.mp4_meta).

[if] an M4A tag is written [then] tinytag and ffprobe see it, audio identical, [else stop].

Base files are real AAC M4As encoded and tagged by ffmpeg, in both layouts
ffmpeg produces: moov after mdat (its default) and moov in front
(``+faststart``), the case where growing moov moves the samples. Every write is
read back by tinytag and by ffprobe (two independent parsers) and the decoded
PCM is hashed before and after.

Regression one-liners:
  - if a written iTunes atom is not seen by tinytag and ffprobe then broken
  - if moov grows in front of mdat and the decoded audio changes then broken
  - if a write that fits moves any byte outside moov then broken
  - if an untouched ilst item is not byte-identical after a write then broken
  - if a failed write leaves the original changed or a temp file behind then broken
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from apps.shared import file_rewrite, mp4_meta, tag_reader
from tests.fixtures import tagged_audio as ta

pytestmark = [pytest.mark.requirement("TAGIO-05"), pytest.mark.requires_ffmpeg]

LAYOUTS = ("m4a", "m4a-faststart")


def _mdat_payload(path: Path) -> bytes:
    raw = path.read_bytes()
    mdat = next(a for a in mp4_meta.iter_atoms(raw, 0, len(raw), str(path)) if a.kind == b"mdat")
    return raw[mdat.payload_start : mdat.end]


def _write_dj_fields(path: Path) -> None:
    meta = mp4_meta.read(path)
    meta.set_tempo(128)
    meta.set_freeform("initialkey", "8A")
    meta.set_freeform("ISRC", "USRC17607839")
    meta.set_freeform("RATING", "4")
    meta.set_freeform("OPENDJ_ENERGY", "7")
    meta.set_text("\xa9cmt", "cue at 1:04 ☾")
    mp4_meta.save(path, meta)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_written_atoms_are_seen_by_two_independent_readers(tmp_path: Path, layout: str) -> None:
    """[if] BPM / key / ISRC / rating / OPENDJ / comment are written [then] tinytag and ffprobe read them, [else stop]."""
    m4a = ta.make_tagged_audio(tmp_path, layout)
    _write_dj_fields(m4a)

    read = tag_reader.read_tags(m4a)
    assert (read.bpm, read.key, read.isrc, read.comment) == (128.0, "8A", "USRC17607839", "cue at 1:04 ☾")
    assert (read.first_other("rating"), read.first_other("opendj_energy")) == ("4", "7")
    assert read.title == ta.STANDARD_TAGS["title"]  # untouched item survives

    probed = ta.ffprobe_tags(m4a)  # ffmpeg does not export tmpo; the free-form atoms it does
    assert (probed["initialkey"], probed["isrc"], probed["rating"], probed["opendj_energy"]) == (
        "8A", "USRC17607839", "4", "7"
    )
    assert probed["comment"] == "cue at 1:04 ☾"
    assert probed["artist"] == ta.STANDARD_TAGS["artist"]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_growing_write_leaves_the_decoded_audio_identical(tmp_path: Path, layout: str) -> None:
    """[if] moov grows past its padding [then] samples and decoded PCM are identical, [else stop]."""
    m4a = ta.make_tagged_audio(tmp_path, layout)
    pcm, samples = ta.decoded_audio_sha256(m4a), _mdat_payload(m4a)
    meta = mp4_meta.read(m4a)
    meta.set_freeform("SERATO_BLOB", "x" * 20_000)  # far beyond any padding
    mp4_meta.save(m4a, meta)
    assert _mdat_payload(m4a) == samples
    assert ta.decoded_audio_sha256(m4a) == pcm
    assert tag_reader.read_tags(m4a).first_other("serato_blob") == "x" * 20_000


def test_faststart_growth_moves_every_chunk_offset(tmp_path: Path) -> None:
    """[if] moov in front of mdat grows by N [then] every stco entry moves by N, [else stop]."""
    m4a = ta.make_tagged_audio(tmp_path, "m4a-faststart")
    before = mp4_meta.read(m4a)
    offsets_before = _chunk_offsets(m4a)
    meta = mp4_meta.read(m4a)
    meta.set_freeform("SERATO_BLOB", "x" * 20_000)
    mp4_meta.save(m4a, meta)
    after = mp4_meta.read(m4a)
    delta = after.moov.end - before.region_end
    assert delta > 0
    assert _chunk_offsets(m4a) == [o + delta for o in offsets_before]


def _chunk_offsets(path: Path) -> list[int]:
    raw = path.read_bytes()
    out: list[int] = []
    at = raw.find(b"stco")
    while at != -1:
        (count,) = struct.unpack_from(">I", raw, at + 8)
        out += list(struct.unpack_from(f">{count}I", raw, at + 12))
        at = raw.find(b"stco", at + 4)
    assert out, "fixture has no stco table"
    return out


def _top_atoms(path: Path) -> list[tuple[bytes, int, bytes]]:
    """``(kind, start, raw)`` of every top-level atom, walked with plain struct reads."""
    raw = path.read_bytes()
    out, pos = [], 0
    while pos < len(raw):
        size, kind = struct.unpack_from(">I4s", raw, pos)
        out.append((kind, pos, raw[pos : pos + size]))
        pos += size
    return out


def _tfhd_base_offsets(path: Path) -> list[int]:
    offsets = []
    for kind, _start, raw in _top_atoms(path):
        at = raw.find(b"tfhd") if kind == b"moof" else -1
        if at != -1 and int.from_bytes(raw[at + 5 : at + 8], "big") & 1:
            offsets.append(struct.unpack_from(">Q", raw, at + 12)[0])
    return offsets


def _tfra_moof_offsets(path: Path) -> list[int]:
    raw = next(r for kind, _s, r in _top_atoms(path) if kind == b"mfra")
    at = raw.find(b"tfra")
    version, width = raw[at + 4], 8 if raw[at + 4] == 1 else 4
    sizes, count = struct.unpack_from(">II", raw, at + 12)
    trailer = sum(((sizes >> s) & 3) + 1 for s in (4, 2, 0))
    fmt = ">Q" if version == 1 else ">I"
    first = at + 20 + width
    return [struct.unpack_from(fmt, raw, first + n * (2 * width + trailer))[0] for n in range(count)]


def test_fragmented_growth_moves_every_fragment_offset(tmp_path: Path) -> None:
    """[if] a fragmented file's moov grows [then] tfhd / tfra offsets follow the moofs, audio identical, [else stop]."""
    m4a = ta.make_tagged_audio(tmp_path, "m4a-fragmented", duration_s=3)
    pcm = ta.decoded_audio_sha256(m4a)
    tfhd_before, tfra_before = _tfhd_base_offsets(m4a), _tfra_moof_offsets(m4a)
    assert len(tfhd_before) > 1 and len(tfra_before) > 1, "fixture is not multi-fragment"
    meta = mp4_meta.read(m4a)
    meta.set_freeform("SERATO_BLOB", "x" * 20_000)
    mp4_meta.save(m4a, meta)

    moofs = [start for kind, start, _raw in _top_atoms(m4a) if kind == b"moof"]
    delta = moofs[0] - tfra_before[0]
    assert delta > 0
    assert _tfra_moof_offsets(m4a) == moofs == [o + delta for o in tfra_before]
    assert _tfhd_base_offsets(m4a) == [o + delta for o in tfhd_before]
    assert ta.decoded_audio_sha256(m4a) == pcm
    assert ta.ffprobe_tags(m4a)["serato_blob"] == "x" * 20_000


def test_a_write_that_fits_changes_nothing_outside_moov(tmp_path: Path) -> None:
    """[if] a write fits the old moov plus padding [then] no byte outside it moves, [else stop]."""
    m4a = ta.make_tagged_audio(tmp_path, "m4a-faststart")
    _write_dj_fields(m4a)  # first write lays down padding
    meta = mp4_meta.read(m4a)
    raw = m4a.read_bytes()
    meta.set_tempo(99)
    meta.set_freeform("OPENDJ_BACKEND_VERSION", "v" * 300)  # grows ilst, well inside the padding
    mp4_meta.save(m4a, meta)
    after = m4a.read_bytes()
    assert len(after) == len(raw)
    assert after[: meta.moov.start] == raw[: meta.moov.start]
    assert after[meta.region_end :] == raw[meta.region_end :]
    assert tag_reader.read_tags(m4a).bpm == 99.0


def test_untouched_items_are_preserved_byte_for_byte(tmp_path: Path) -> None:
    """[if] one item is replaced [then] every other item is byte-identical and in order, [else stop]."""
    m4a = ta.make_tagged_audio(tmp_path, "m4a")
    before = [item.raw for item in mp4_meta.read(m4a).items]
    meta = mp4_meta.read(m4a)
    meta.set_text("\xa9gen", "Dub Techno")
    mp4_meta.save(m4a, meta)
    after = mp4_meta.read(m4a)
    gen_at = [item.kind for item in after.items].index(b"\xa9gen")
    assert after.items[gen_at].value() == "Dub Techno"
    assert [raw for n, raw in enumerate(i.raw for i in after.items) if n != gen_at] == [
        raw for raw in before if raw[4:8] != b"\xa9gen"
    ]


def test_a_freeform_name_is_replaced_case_insensitively(tmp_path: Path) -> None:
    """[if] INITIALKEY is set over an existing initialkey [then] one atom remains, [else stop]."""
    m4a = ta.make_tagged_audio(tmp_path, "m4a")
    meta = mp4_meta.read(m4a)
    meta.set_freeform("initialkey", "1A")
    mp4_meta.save(m4a, meta)
    meta = mp4_meta.read(m4a)
    meta.set_freeform("INITIALKEY", "2B")
    mp4_meta.save(m4a, meta)
    names = [i.freeform_name() for i in mp4_meta.read(m4a).items if i.freeform_name()]
    assert names == [(mp4_meta.ITUNES_MEAN, "INITIALKEY")]
    assert tag_reader.read_tags(m4a).key == "2B"


def test_a_file_with_no_metadata_gets_an_ilst(tmp_path: Path) -> None:
    """[if] an M4A has no udta/meta/ilst [then] one is created and read back, [else stop]."""
    m4a = ta.make_untagged_audio(tmp_path, "m4a")
    pcm = ta.decoded_audio_sha256(m4a)
    meta = mp4_meta.read(m4a)
    meta.set_tempo(126)
    meta.set_freeform("ISRC", "GBABC2600002")
    mp4_meta.save(m4a, meta)
    read = tag_reader.read_tags(m4a)
    assert (read.bpm, read.isrc) == (126.0, "GBABC2600002")
    assert ta.ffprobe_tags(m4a)["isrc"] == "GBABC2600002"
    assert ta.decoded_audio_sha256(m4a) == pcm


def test_a_failed_write_leaves_the_original_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] writing fails midway [then] the original file is byte-identical, [else stop]."""
    m4a = ta.make_tagged_audio(tmp_path, "m4a-faststart")
    before = m4a.read_bytes()
    real_fsync = file_rewrite.os.fsync

    def failing_fsync(fd: int) -> None:
        real_fsync(fd)
        raise OSError("disk full")

    monkeypatch.setattr(file_rewrite.os, "fsync", failing_fsync)
    meta = mp4_meta.read(m4a)
    meta.set_freeform("SERATO_BLOB", "x" * 20_000)
    with pytest.raises(OSError, match="disk full"):
        mp4_meta.save(m4a, meta)
    assert m4a.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [m4a.name]


def test_a_non_mp4_file_is_refused(tmp_path: Path) -> None:
    """[if] the file is not MP4 [then] Mp4Error, nothing written, [else stop]."""
    mp3 = ta.make_tagged_audio(tmp_path, "mp3-v24")
    before = mp3.read_bytes()
    with pytest.raises(mp4_meta.Mp4Error, match="not an MP4 file"):
        mp4_meta.read(mp3)
    assert mp3.read_bytes() == before


def test_a_file_changed_since_it_was_read_is_refused(tmp_path: Path) -> None:
    """[if] the file's layout changed after read [then] save refuses, [else stop]."""
    m4a = ta.make_tagged_audio(tmp_path, "m4a")
    stale = mp4_meta.read(m4a)
    fresh = mp4_meta.read(m4a)
    fresh.set_freeform("SERATO_BLOB", "x" * 20_000)
    mp4_meta.save(m4a, fresh)
    stale.set_tempo(1)
    with pytest.raises(mp4_meta.Mp4Error, match="changed on disk"):
        mp4_meta.save(m4a, stale)
