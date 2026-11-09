"""Byte builders for Serato tag fixture files (FLAC, MP4, AIFF, MP3). Invented names only."""

from __future__ import annotations

import base64
import struct
from pathlib import Path

from apps.adapters.serato.geob import (
    GEOB_BEATGRID_DESC,
    GEOB_MARKERS2_DESC,
    BeatGrid,
    BeatGridMarker,
    Markers2,
    Markers2Cue,
    Markers2Loop,
    encode_beatgrid,
    encode_markers2,
)
from apps.shared import mp4_meta, vorbis_comment
from apps.shared.mp4_meta import FREEFORM, atom

MP4_SERATO_MEAN = "com.serato.dj"
MP4_MARKERS2_NAME = "markersv2"
MP4_BEATGRID_NAME = "beatgrid"
FLAC_MARKERS2_KEY = "SERATO_MARKERS_V2"
FLAC_BEATGRID_KEY = "SERATO_BEATGRID"


def markers2_payload() -> bytes:
    return encode_markers2(
        Markers2(
            cues=(
                Markers2Cue(index=0, position_ms=1000, color_rgb=0xCC0000, name="Cue A"),
                Markers2Cue(index=1, position_ms=2500, color_rgb=0x00CC00, name="Cue B"),
            ),
            loops=(
                Markers2Loop(
                    index=0,
                    start_ms=500,
                    end_ms=3000,
                    color_rgb=0x2222DD,
                    name="Loop 1",
                    locked=False,
                ),
            ),
        )
    )


def beatgrid_payload() -> bytes:
    return encode_beatgrid(BeatGrid(markers=(BeatGridMarker(position_seconds=0.0, bpm=128.0),)))


def geob_header(desc: str) -> bytes:
    return b"application/octet-stream\x00\x00" + desc.encode("ascii") + b"\x00"


def serato_text(desc: str, payload: bytes, *, wrap: int = 72) -> str:
    raw = base64.b64encode(geob_header(desc) + payload).rstrip(b"=").decode("ascii")
    if wrap <= 0:
        return raw
    lines = [raw[i : i + wrap] for i in range(0, len(raw), wrap)]
    return "\n".join(lines)


def write_flac(path: Path, comments: list[tuple[str, str]]) -> None:
    streaminfo = b"\x00" * 34
    block0 = bytes([0x00]) + len(streaminfo).to_bytes(3, "big") + streaminfo
    vorbis = vorbis_comment.encode("Serato DJ Pro", comments)
    block_v = bytes([0x80 | 4]) + len(vorbis).to_bytes(3, "big") + vorbis
    path.write_bytes(b"fLaC" + block0 + block_v)


def _freeform_item(mean: str, name: str, value: bytes, *, data_type: int = mp4_meta.DATA_UTF8) -> bytes:
    data = struct.pack(">II", data_type, 0) + value
    payload = (
        atom(b"mean", b"\x00\x00\x00\x00" + mean.encode("utf-8"))
        + atom(b"name", b"\x00\x00\x00\x00" + name.encode("utf-8"))
        + atom(b"data", data)
    )
    return atom(FREEFORM, payload)


def write_mp4(path: Path, freeforms: list[tuple[str, str, bytes]], *, data_type: int = mp4_meta.DATA_UTF8) -> None:
    ilst = b"".join(_freeform_item(mean, name, val, data_type=data_type) for mean, name, val in freeforms)
    meta = mp4_meta._HDLR_MDIR + atom(b"ilst", ilst)
    moov = atom(b"moov", atom(b"udta", atom(b"meta", meta)))
    ftyp = atom(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")
    path.write_bytes(ftyp + moov)


def write_mp4_text_only(path: Path) -> None:
    nam = atom(b"\xa9nam", struct.pack(">II", mp4_meta.DATA_UTF8, 0) + b"Invented")
    meta = mp4_meta._HDLR_MDIR + atom(b"ilst", nam)
    moov = atom(b"moov", atom(b"udta", atom(b"meta", meta)))
    ftyp = atom(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")
    path.write_bytes(ftyp + moov)


def write_mp4_no_udta(path: Path) -> None:
    moov = atom(b"moov", b"")
    ftyp = atom(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")
    path.write_bytes(ftyp + moov)


def _aiff_chunk(chunk_id: bytes, payload: bytes) -> bytes:
    padded = payload + (b"\x00" if len(payload) & 1 else b"")
    return chunk_id + struct.pack(">I", len(payload)) + padded


def _id3_tag_with_geob(*payloads: tuple[str, bytes]) -> bytes:
    frames = b""
    for desc, payload in payloads:
        body = b"\x00" + b"application/octet-stream\x00" + b"\x00" + desc.encode("ascii") + b"\x00" + payload
        frames += b"GEOB" + len(body).to_bytes(4, "big") + b"\x00\x00" + body
    syncsafe = bytes(((len(frames) >> shift) & 0x7F) for shift in (21, 14, 7, 0))
    return b"ID3\x03\x00\x00" + syncsafe + frames


def write_mp3_with_serato(path: Path, markers2: bytes, beatgrid: bytes | None = None) -> None:
    payloads: list[tuple[str, bytes]] = [(GEOB_MARKERS2_DESC, markers2)]
    if beatgrid is not None:
        payloads.append((GEOB_BEATGRID_DESC, beatgrid))
    tag = _id3_tag_with_geob(*payloads)
    path.write_bytes(tag + b"\xff\xfb\x90\x00" + b"\x00" * 16)


def write_aiff_with_id3(path: Path, markers2: bytes, beatgrid: bytes | None = None) -> None:
    payloads: list[tuple[str, bytes]] = [(GEOB_MARKERS2_DESC, markers2)]
    if beatgrid is not None:
        payloads.append((GEOB_BEATGRID_DESC, beatgrid))
    tag = _id3_tag_with_geob(*payloads)
    body = _aiff_chunk(b"COMM", b"\x00" * 18) + _aiff_chunk(b"ID3 ", tag)
    form_size = 4 + len(body)
    path.write_bytes(struct.pack(">4sI4s", b"FORM", form_size, b"AIFF") + body)


def write_aiff_no_id3(path: Path) -> None:
    body = _aiff_chunk(b"COMM", b"\x00" * 18)
    form_size = 4 + len(body)
    path.write_bytes(struct.pack(">4sI4s", b"FORM", form_size, b"AIFF") + body)


def write_aiff_bad_id3_chunk(path: Path) -> None:
    body = _aiff_chunk(b"COMM", b"\x00" * 18) + _aiff_chunk(b"ID3 ", b"not an id3 tag")
    form_size = 4 + len(body)
    path.write_bytes(struct.pack(">4sI4s", b"FORM", form_size, b"AIFF") + body)
