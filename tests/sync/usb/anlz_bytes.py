"""Synthetic rekordbox ANLZ (PMAI) bytes for the stick analysis tests.

Every tag is built byte by byte from the layouts pyrekordbox documents
(``pyrekordbox/anlz/structs.py``) and the PCO2/PCP2 layout Deep Symmetry
documents. Nothing here is copied from a real stick: the repo is public.

-Claude
"""

from __future__ import annotations

import struct
from pathlib import Path

from pyrekordbox.anlz.file import XOR_MASK


def _tag(fourcc: bytes, header_rest: bytes, body: bytes = b"") -> bytes:
    head_len = 12 + len(header_rest)
    return fourcc + struct.pack(">II", head_len, head_len + len(body)) + header_rest + body


def pmai(*tags: bytes) -> bytes:
    body = b"".join(tags)
    return b"PMAI" + struct.pack(">II", 28, 28 + len(body)) + bytes(16) + body


def pqtz(beats: list[tuple[int, float, int]]) -> bytes:
    """beats = (beat in bar 1..4, bpm, time_ms)."""
    body = b"".join(struct.pack(">HHI", n, round(bpm * 100), t) for n, bpm, t in beats)
    return _tag(b"PQTZ", bytes(4) + struct.pack(">II", 0x80000, len(beats)), body)


def grid(first_ms: int, bpm: float, count: int) -> list[tuple[int, float, int]]:
    step = 60000 / bpm
    return [(i % 4 + 1, bpm, round(first_ms + i * step)) for i in range(count)]


def pcpt(hot: int, time_ms: int, loop_ms: int | None = None) -> bytes:
    return struct.pack(
        ">4sIIIIIHHBxHII16x",
        b"PCPT",
        28,
        56,
        hot,
        4 if hot else 0,
        0x10000,
        0xFFFF,
        0xFFFF,
        2 if loop_ms is not None else 1,
        1000,
        time_ms,
        0xFFFFFFFF if loop_ms is None else loop_ms,
    )


def pcob(list_type: int, *entries: bytes) -> bytes:
    return _tag(b"PCOB", struct.pack(">IHHi", list_type, 0, len(entries), -1), b"".join(entries))


def pcp2(
    hot: int,
    time_ms: int,
    *,
    comment: str = "",
    color: int | None = None,
    loop_ms: int | None = None,
) -> bytes:
    """One PCP2 entry; ``color=None`` ends it after the comment, with no color bytes
    (44 bytes when there is no comment either); a color pads it to rekordbox's 88."""
    comment_bytes = (comment + "\0").encode("utf-16-be") if comment else b""
    tail = bytes([color, 0xFF, 0x00, 0x17]) if color is not None else b""
    entry_len = 44 + len(comment_bytes) + len(tail)
    if color is not None:
        entry_len = max(entry_len, 88)
    head = struct.pack(
        ">4sIIIB3xIIB7xHHI",
        b"PCP2",
        16,
        entry_len,
        hot,
        1 if loop_ms is None else 2,
        time_ms,
        0xFFFFFFFF if loop_ms is None else loop_ms,
        0,
        0,
        0,
        len(comment_bytes),
    )
    entry = head + comment_bytes + tail
    return entry + bytes(entry_len - len(entry))


def pco2(list_type: int, *entries: bytes) -> bytes:
    return _tag(b"PCO2", struct.pack(">IHH", list_type, len(entries), 0), b"".join(entries))


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


def pssi(phrases: list[tuple[int, int]], *, end_beat: int, masked: bool) -> bytes:
    """phrases = (start beat, kind). ``masked`` applies rekordbox's export XOR."""
    entries = b"".join(
        struct.pack(">HHH6B3H4BH", i + 1, beat, kind, *([0] * 14))
        for i, (beat, kind) in enumerate(phrases)
    )
    header_rest = struct.pack(
        ">IHH6sH2sB1s", 24, len(phrases), 1, bytes(6), end_beat, bytes(2), 1, bytes(1)
    )
    tag = bytearray(_tag(b"PSSI", header_rest, entries))
    if masked:
        for index in range(len(tag) - 18):
            tag[18 + index] ^= (XOR_MASK[index % len(XOR_MASK)] + len(phrases)) % 256
    return bytes(tag)


def write_track(
    volume: Path, rel_dat: str, *, dat: bytes, ext: bytes | None = None, twoex: bytes | None = None
) -> str:
    """Write one track's files; returns the pdb-style analyze_path."""
    dat_path = volume / rel_dat
    dat_path.parent.mkdir(parents=True, exist_ok=True)
    dat_path.write_bytes(dat)
    if ext is not None:
        dat_path.with_suffix(".EXT").write_bytes(ext)
    if twoex is not None:
        dat_path.with_suffix(".2EX").write_bytes(twoex)
    return "/" + rel_dat


__all__ = [
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
