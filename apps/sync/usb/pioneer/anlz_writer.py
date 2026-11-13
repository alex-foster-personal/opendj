"""Encode rekordbox stick ANLZ files and synthetic PMAI bytes for classic USB export.

Production ``build_dat`` / ``build_ext`` use library analysis inputs (USBEXP-10746).
Classic directory export uses :func:`build_dat_ext` and :func:`write_track` with
simpler :class:`TrackAnlzInput`. Tag layouts follow pyrekordbox and Deep Symmetry.
Low-level public builders (``pmai``, ``pqtz``, ``pwav``, ...) support stick tests.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
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


def pmai(*tags: bytes) -> bytes:
    return _pmai(*tags)


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


def pcpt(hot: int, time_ms: int, loop_ms: int | None = None) -> bytes:
    return _pcpt(hot, time_ms, loop_ms)


def _pcob(list_name: bytes, entries: bytes) -> bytes:
    count = len(entries) // 56 if entries else 0
    return _tag(b"PCOB", struct.pack(">IHHi", 0 if list_name == b"memory" else 1, 0, count, -1), entries)


def pcob(list_type: int, *entries: bytes) -> bytes:
    return _pcob(b"memory" if list_type == _MEMORY_LIST else b"hotcue", b"".join(entries))


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


def pcp2(
    hot: int,
    time_ms: int,
    *,
    comment: str = "",
    color: int | None = None,
    loop_ms: int | None = None,
) -> bytes:
    """``hot`` is the on-stick hot index (0 = memory, 1..8 = pads A..H)."""
    if hot == 0:
        cue = CueInput("memory", 0, time_ms, comment=comment or None, color=color, loop_end_ms=loop_ms)
    else:
        cue = CueInput("hot", hot - 1, time_ms, comment=comment or None, color=color, loop_end_ms=loop_ms)
    return _pcp2(cue)


def _pco2(list_type: int, entries: tuple[bytes, ...]) -> bytes:
    body = b"".join(entries)
    return _tag(b"PCO2", struct.pack(">IHH", list_type, len(entries), 0), body)


def pco2(list_type: int, *entries: bytes) -> bytes:
    return _pco2(list_type, entries)


# ----- waveforms (production detail) ------------------------------------------


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


# ----- waveforms (synthetic fixtures) -----------------------------------------


def _cols(n: int, width: int, seed: int) -> bytes:
    return bytes((i * 7 + seed * 13 + (i // width) * 3) % 128 for i in range(n * width))


def pwv6(n: int, seed: int) -> bytes:
    return _tag(b"PWV6", struct.pack(">II", 3, n), _cols(n, 3, seed))


def pwv7(n: int, seed: int) -> bytes:
    return _tag(b"PWV7", struct.pack(">III", 3, n, 0x00960000), _cols(n, 3, seed))


def pwav(n: int, seed: int) -> bytes:
    return _tag(b"PWAV", struct.pack(">II", n, 0x10000), _cols(n, 1, seed))


def pwv3(n: int, seed: int) -> bytes:
    return _tag(b"PWV3", struct.pack(">III", 1, n, 0x00960000), _cols(n, 1, seed))


def pwv5(heights: list[int]) -> bytes:
    body = b"".join(struct.pack(">H", (0b101 << 13) | (h << 2)) for h in heights)
    return _tag(b"PWV5", struct.pack(">III", 2, len(heights), 0), body)


def pvdi(envelope: bytes) -> bytes:
    fixed = bytes.fromhex("0000040056220001") + struct.pack(">I", len(envelope))
    return _tag(b"PVDI", fixed, envelope)


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


def pssi(phrases: list[tuple[int, int]], *, end_beat: int, masked: bool) -> bytes:
    entries = b"".join(
        struct.pack(">HHH6B3H4BH", i + 1, beat, kind, *([0] * 14)) for i, (beat, kind) in enumerate(phrases)
    )
    header_rest = struct.pack(">IHH6sH2sB1s", 24, len(phrases), 1, bytes(6), end_beat, bytes(2), 1, bytes(1))
    tag = bytearray(_tag(b"PSSI", header_rest, entries))
    if masked:
        for index in range(len(tag) - 18):
            tag[18 + index] ^= (XOR_MASK[index % len(XOR_MASK)] + len(phrases)) % 256
    return bytes(tag)


# ----- PQTZ grid --------------------------------------------------------------


def _pqtz_tag(beats: tuple[BeatInput, ...]) -> bytes:
    body = b"".join(struct.pack(">HHI", b.beat_in_bar, round(b.bpm * 100), b.time_ms) for b in beats)
    return _tag(b"PQTZ", bytes(4) + struct.pack(">II", 0x80000, len(beats)), body)


def pqtz(beats: list[tuple[int, float, int]]) -> bytes:
    """beats = (beat in bar 1..4, bpm, time_ms)."""
    body = b"".join(struct.pack(">HHI", n, round(bpm * 100), t) for n, bpm, t in beats)
    return _tag(b"PQTZ", bytes(4) + struct.pack(">II", 0x80000, len(beats)), body)


def grid(first_ms: int, bpm: float, count: int) -> list[tuple[int, float, int]]:
    step = 60000 / bpm
    return [(i % 4 + 1, bpm, round(first_ms + i * step)) for i in range(count)]


# ----- public writers (library analysis) --------------------------------------


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
    pssi_tag = _pssi_tag(track)
    if pssi_tag is not None:
        tags.append(pssi_tag)
    return _pmai(*tags)


def build_anlz_pair(track: TrackAnalysisInput) -> tuple[bytes, bytes]:
    """Return (.DAT, .EXT) bytes for one stick track directory."""
    return build_dat(track), build_ext(track)


# ----- classic directory export (USBEXP-10743) --------------------------------


def write_track(volume: Path, rel_dat: str, *, dat: bytes, ext: bytes | None = None, twoex: bytes | None = None) -> str:
    """Write one track's files; returns the pdb-style analyze_path."""
    dat_path = volume / rel_dat
    dat_path.parent.mkdir(parents=True, exist_ok=True)
    dat_path.write_bytes(dat)
    if ext is not None:
        dat_path.with_suffix(".EXT").write_bytes(ext)
    if twoex is not None:
        dat_path.with_suffix(".2EX").write_bytes(twoex)
    return "/" + rel_dat.replace("\\", "/")


@dataclass(frozen=True)
class TrackAnlzInput:
    bpm: float
    first_ms: int = 25
    beat_count: int = 32
    hot_cue_ms: int = 1000
    hot_cue_pad: int = 1
    seed: int = 1


def build_dat_ext(analysis: TrackAnlzInput) -> tuple[bytes, bytes]:
    beats = grid(analysis.first_ms, analysis.bpm, analysis.beat_count)
    dat = pmai(pqtz(beats), pwav(400, analysis.seed))
    ext = pmai(
        pco2(1, pcp2(analysis.hot_cue_pad, analysis.hot_cue_ms, color=43)),
        pco2(0, pcp2(0, max(0, analysis.hot_cue_ms // 2))),
    )
    return dat, ext


def analyze_paths(*, pdb_track_id: int, content_sha256_hex: str) -> tuple[str, str]:
    """Return ``(rel_dat_path, pdb_analyze_path)`` with a unique ANLZ directory per track."""
    digest = content_sha256_hex[:8].upper()
    pool = f"P{pdb_track_id:03d}"
    rel = f"PIONEER/USBANLZ/{pool}/{digest}/ANLZ0000.DAT"
    return rel, f"/{rel}"


__all__ = [
    "BeatInput",
    "CueInput",
    "PhraseInput",
    "TrackAnalysisInput",
    "TrackAnlzInput",
    "analyze_paths",
    "build_anlz_pair",
    "build_dat",
    "build_dat_ext",
    "build_ext",
    "grid",
    "pco2",
    "pcob",
    "pcp2",
    "pcpt",
    "pmai",
    "pqtz",
    "pssi",
    "pvdi",
    "pwav",
    "pwv3",
    "pwv5",
    "pwv6",
    "pwv7",
    "write_track",
]
