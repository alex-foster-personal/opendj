"""Encode rekordbox stick ANLZ files from a library track analysis (USB export).

Writes byte-perfect PMAI containers using the tag layouts documented by
pyrekordbox and Deep Symmetry. No whole-file pyrekordbox parse is used.

-Cursor-cloud
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Literal

import numpy as np

from pyrekordbox.anlz.file import XOR_MASK

_DETAIL_RATE: int = 150
_HOT_LIST: int = 1
_MEMORY_LIST: int = 0


@dataclass(frozen=True)
class BeatInput:
    beat_in_bar: int
    bpm: float
    time_ms: int


@dataclass(frozen=True)
class CueInput:
    """One cue for PCO2 (.EXT) or PCOB (.DAT). Hot pads are 0..7 (A..H)."""

    kind: Literal["hot", "memory"]
    pad: int
    position_ms: int
    comment: str | None = None
    color: int | None = None
    loop_end_ms: int | None = None


@dataclass(frozen=True)
class PhraseInput:
    start_beat: int
    kind: int


@dataclass(frozen=True)
class TrackAnalysisInput:
    duration_s: float
    beats: tuple[BeatInput, ...]
    hot_cues: tuple[CueInput, ...]
    memory_cues: tuple[CueInput, ...]
    detail_low: tuple[int, ...]
    detail_mid: tuple[int, ...]
    detail_high: tuple[int, ...]
    phrases: tuple[PhraseInput, ...] | None
    phrase_end_beat: int = 0
    phrase_mood: int = 1


# ----- PMAI container ---------------------------------------------------------


def _tag(fourcc: bytes, header_rest: bytes, body: bytes = b"") -> bytes:
    head_len = 12 + len(header_rest)
    return fourcc + struct.pack(">II", head_len, head_len + len(body)) + header_rest + body


def _pmai(*tags: bytes) -> bytes:
    body = b"".join(tags)
    return b"PMAI" + struct.pack(">II", 28, 28 + len(body)) + bytes(16) + body


# ----- shared validation ------------------------------------------------------


def _checked_pad(pad: int) -> int:
    if not 0 <= pad <= 7:
        raise ValueError(f"hot cue pad must be 0..7, got {pad}")
    return pad


def _hot_index(cue: CueInput) -> int:
    if cue.kind == "memory":
        return 0
    return _checked_pad(cue.pad) + 1


def _loop_kind(loop_end_ms: int | None) -> int:
    return 1 if loop_end_ms is None else 2


def _loop_time(loop_end_ms: int | None) -> int:
    return 0xFFFFFFFF if loop_end_ms is None else loop_end_ms


# ----- PCOB / PCPT (.DAT legacy cues) -----------------------------------------


def _pcpt(hot_index: int, time_ms: int, loop_end_ms: int | None) -> bytes:
    return struct.pack(
        ">4sIIIIIHHBxHII16x",
        b"PCPT",
        28,
        56,
        hot_index,
        4 if hot_index else 0,
        0x10000,
        0xFFFF,
        0xFFFF,
        _loop_kind(loop_end_ms),
        1000,
        time_ms,
        _loop_time(loop_end_ms),
    )


def _pcob(list_name: bytes, entries: bytes) -> bytes:
    count = len(entries) // 56 if entries else 0
    return _tag(b"PCOB", struct.pack(">IHHi", 0 if list_name == b"memory" else 1, 0, count, -1), entries)


# ----- PCO2 / PCP2 (.EXT extended cues) ---------------------------------------


def _pcp2(cue: CueInput) -> bytes:
    comment_bytes = (cue.comment + "\0").encode("utf-16-be") if cue.comment else b""
    tail = bytes([cue.color, 0xFF, 0x00, 0x17]) if cue.color is not None else b""
    entry_len = 44 + len(comment_bytes) + len(tail)
    if cue.color is not None:
        entry_len = max(entry_len, 88)
    head = struct.pack(
        ">4sIIIB3xIIB7xHHI",
        b"PCP2",
        16,
        entry_len,
        _hot_index(cue),
        _loop_kind(cue.loop_end_ms),
        cue.position_ms,
        _loop_time(cue.loop_end_ms),
        0,
        0,
        0,
        len(comment_bytes),
    )
    entry = head + comment_bytes + tail
    return entry + bytes(entry_len - len(entry))


def _pco2(list_type: int, entries: tuple[bytes, ...]) -> bytes:
    body = b"".join(entries)
    return _tag(b"PCO2", struct.pack(">IHH", list_type, len(entries), 0), body)


# ----- waveforms --------------------------------------------------------------


def _detail_column_count(duration_s: float) -> int:
    if duration_s < 0:
        raise ValueError(f"duration_s must be >= 0, got {duration_s}")
    return round(duration_s * _DETAIL_RATE)


def _bucket_max(values: tuple[int, ...], columns: int) -> np.ndarray:
    arr = np.asarray(values, dtype=np.int64)
    if arr.size == 0:
        return np.zeros(columns, dtype=np.uint8)
    if arr.size == columns:
        return np.clip(arr, 0, 31).astype(np.uint8)
    if columns <= 0:
        return np.zeros(0, dtype=np.uint8)
    edges = (np.arange(columns) * arr.size) // columns
    return np.clip(np.maximum.reduceat(arr, edges), 0, 31).astype(np.uint8)


def _pwv3_body(low: np.ndarray, mid: np.ndarray, high: np.ndarray) -> bytes:
    height = np.maximum(np.maximum(low, mid), high)
    whiteness = np.where(high >= np.maximum(low, mid), 7, 4).astype(np.uint8)
    packed = (whiteness << 5) | height
    return packed.tobytes()


def _pwv5_body(low: np.ndarray, mid: np.ndarray, high: np.ndarray) -> bytes:
    height = np.maximum(np.maximum(low, mid), high)
    words: list[int] = []
    for lo, md, hi, h in zip(low, mid, high, height, strict=True):
        r = min(7, int(hi) >> 2)
        g = min(7, int(md) >> 2)
        b = min(7, int(lo) >> 2)
        words.append((r << 13) | (g << 10) | (b << 7) | (int(h) << 2))
    return b"".join(struct.pack(">H", word) for word in words)


def _detail_bands(track: TrackAnalysisInput) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = _detail_column_count(track.duration_s)
    len_low = len(track.detail_low)
    len_mid = len(track.detail_mid)
    len_high = len(track.detail_high)
    if not (len_low == len_mid == len_high):
        raise ValueError("detail_low, detail_mid and detail_high must have equal lengths")
    return (
        _bucket_max(track.detail_low, n),
        _bucket_max(track.detail_mid, n),
        _bucket_max(track.detail_high, n),
    )


def _pwv3_tag(low: np.ndarray, mid: np.ndarray, high: np.ndarray) -> bytes:
    body = _pwv3_body(low, mid, high)
    n = len(body)
    header = struct.pack(">III", 1, n, 0x00960000)
    return _tag(b"PWV3", header, body)


def _pwv5_tag(low: np.ndarray, mid: np.ndarray, high: np.ndarray) -> bytes:
    body = _pwv5_body(low, mid, high)
    n = len(body) // 2
    header = struct.pack(">III", 2, n, 0)
    return _tag(b"PWV5", header, body)


# ----- PSSI phrases -----------------------------------------------------------


def _pssi_tag(track: TrackAnalysisInput) -> bytes | None:
    if not track.phrases:
        return None
    entries = b"".join(
        struct.pack(">HHH6B3H4BH", i + 1, phrase.start_beat, phrase.kind, *([0] * 14))
        for i, phrase in enumerate(track.phrases)
    )
    header_rest = struct.pack(
        ">IHH6sH2sB1s",
        24,
        len(track.phrases),
        track.phrase_mood,
        bytes(6),
        track.phrase_end_beat,
        bytes(2),
        1,
        bytes(1),
    )
    tag = bytearray(_tag(b"PSSI", header_rest, entries))
    n = len(track.phrases)
    for index in range(len(tag) - 18):
        tag[18 + index] ^= (XOR_MASK[index % len(XOR_MASK)] + n) % 256
    return bytes(tag)


# ----- PQTZ grid --------------------------------------------------------------


def _pqtz_tag(beats: tuple[BeatInput, ...]) -> bytes:
    body = b"".join(struct.pack(">HHI", b.beat_in_bar, round(b.bpm * 100), b.time_ms) for b in beats)
    return _tag(b"PQTZ", bytes(4) + struct.pack(">II", 0x80000, len(beats)), body)


# ----- public writers ---------------------------------------------------------


def build_dat(track: TrackAnalysisInput) -> bytes:
    """Write ANLZ0000.DAT: PQTZ and legacy PCOB cue lists (no PCO2)."""
    hot = b"".join(_pcpt(_hot_index(c), c.position_ms, c.loop_end_ms) for c in track.hot_cues)
    mem = b"".join(_pcpt(0, c.position_ms, c.loop_end_ms) for c in track.memory_cues)
    tags = [_pqtz_tag(track.beats)]
    tags.append(_pcob(b"hotcue", hot))
    tags.append(_pcob(b"memory", mem))
    return _pmai(*tags)


def build_ext(track: TrackAnalysisInput) -> bytes:
    """Write ANLZ0000.EXT: PCO2 cues, PWV3/PWV5 detail waveforms and optional PSSI."""
    low, mid, high = _detail_bands(track)
    n = len(low)
    expected = _detail_column_count(track.duration_s)
    if n != expected:
        raise ValueError(f"PWV3 length must be {expected} columns, got {n}")
    hot = tuple(_pcp2(c) for c in track.hot_cues)
    mem = tuple(_pcp2(c) for c in track.memory_cues)
    tags: list[bytes] = [
        _pco2(_HOT_LIST, hot),
        _pco2(_MEMORY_LIST, mem),
        _pwv3_tag(low, mid, high),
        _pwv5_tag(low, mid, high),
    ]
    pssi = _pssi_tag(track)
    if pssi is not None:
        tags.append(pssi)
    return _pmai(*tags)


def build_anlz_pair(track: TrackAnalysisInput) -> tuple[bytes, bytes]:
    """Return (.DAT, .EXT) bytes for one stick track directory."""
    return build_dat(track), build_ext(track)


__all__ = [
    "BeatInput",
    "CueInput",
    "PhraseInput",
    "TrackAnalysisInput",
    "build_anlz_pair",
    "build_dat",
    "build_ext",
]
