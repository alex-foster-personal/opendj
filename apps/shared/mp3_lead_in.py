"""MP3 encoder lead-in: the samples our decoders trim and rekordbox keeps.

An MP3 encoder pads the start of the stream (the encoder delay, written by LAME
and by ffmpeg's Lavc/Lavf into the LAME extension of the first frame's
Xing/Info tag) and the decoder adds 529 samples of its own. Gapless decoders
drop both: symphonia (the Rust engine and ``odj-audio decode``), ffmpeg's
default decode and Chromium's ``decodeAudioData`` all start the track that
many samples in. rekordbox does not. Its time zero is the first sample of a
raw decode, so every cue, beat and phrase it stores for such a file sits the
lead-in later than the same audio on our timeline. Measured Thu 1 Oct 2026 on
32 library MP3s against rekordbox's own waveforms: 0.00 ms between rekordbox
and a raw decode on 30 of them, every header class; 1105 samples (25.06 ms
at 44.1 kHz) is the usual lead-in. ADR: ``docs/decisions/ADR-NEW-mp3-lead-in-shift-at-import.md``.

So rekordbox positions are shifted at the boundary (JIK, Fri 2 Oct 2026,
"Shift at import"): minus the lead-in coming in, plus the lead-in going back.

This reader mirrors symphonia 0.6.1's own decision
(``symphonia-bundle-mp3`` ``demuxer.rs``, ``MpaReader::try_new`` and
``try_read_info_tag_inner``) rather than a general MP3 parser, because the
number that matters is exactly what the engine trims: the first frame that
syncs and is followed by a similar frame, a Layer 3 Xing/Info tag at
``4 + side_info_len`` with zeroed side information, a LAME extension of at
least 24 bytes whose encoder starts ``LAME``, ``Lavf`` or ``Lavc``, and a tag
CRC that matches (or is absent, or 0). Anything else trims nothing, so the
lead-in is 0.

-Claude
"""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path

from apps.shared import fs_residency, platform_paths

#: Decoder delay every MPEG Layer 3 decoder adds, on top of the encoder's.
DECODER_DELAY = 529
#: How far into the file the first frame is searched for, after any ID3v2 tag.
#: symphonia scans without a bound; real files put the first frame right after
#: the tag, and junk longer than this means a file no reader here should guess at.
_MAX_SYNC_SCAN = 64 * 1024
_HEADER_LEN = 4
_LAME_ENCODERS = (b"LAME", b"Lavf", b"Lavc")
_MIN_LAME_EXT_LEN = 24
_LAME_EXT_LEN = 36

_BITRATES_MPEG1_L1 = (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448)
_BITRATES_MPEG1_L2 = (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384)
_BITRATES_MPEG1_L3 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_BITRATES_MPEG2_L1 = (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256)
_BITRATES_MPEG2_L23 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
_SAMPLE_RATES = {
    3: (44_100, 48_000, 32_000),  # MPEG 1
    2: (22_050, 24_000, 16_000),  # MPEG 2
    0: (11_025, 12_000, 8_000),  # MPEG 2.5
}


@dataclass(frozen=True)
class LeadIn:
    """What a gapless decoder trims from the start of one file."""

    #: Samples per channel trimmed (encoder delay plus ``DECODER_DELAY``), 0 when none.
    frames: int
    #: The stream's sample rate, or None when the file is not an MP3 stream.
    sample_rate: int | None
    #: The LAME extension's encoder field (``LAME3.100``, ``Lavc58.91``), or None.
    encoder: str | None = None

    @property
    def seconds(self) -> float:
        if not self.frames or not self.sample_rate:
            return 0.0
        return self.frames / self.sample_rate


NO_LEAD_IN = LeadIn(frames=0, sample_rate=None)


@dataclass(frozen=True)
class _Header:
    version: int  # 3 = MPEG 1, 2 = MPEG 2, 0 = MPEG 2.5
    layer: int  # 1, 2 or 3
    has_crc: bool
    sample_rate: int
    mono: bool
    frame_len: int  # bytes, header included

    @property
    def header_size(self) -> int:
        return _HEADER_LEN + (2 if self.has_crc else 0)

    @property
    def side_info_len(self) -> int:
        if self.version == 3:
            return 17 if self.mono else 32
        return 9 if self.mono else 17


def _check_header(word: int) -> bool:
    """symphonia's ``check_header``: cheap rejections of a candidate sync word."""
    return not (
        (word >> 19) & 0x3 == 0x1
        or (word >> 17) & 0x3 == 0x0
        or (word >> 12) & 0xF == 0xF
        or (word >> 10) & 0x3 == 0x3
    )


def _parse_header(word: int) -> _Header | None:
    """symphonia's ``parse_frame_header``, or None where it returns an error."""
    if word & 0xFFE0_0000 != 0xFFE0_0000 or not _check_header(word):
        return None
    version = (word >> 19) & 0x3
    layer = {0b01: 3, 0b10: 2, 0b11: 1}[(word >> 17) & 0x3]
    br_index = (word >> 12) & 0xF
    if br_index == 0:
        return None  # free bit-rate: symphonia refuses it
    if version == 3:
        table = {1: _BITRATES_MPEG1_L1, 2: _BITRATES_MPEG1_L2, 3: _BITRATES_MPEG1_L3}[layer]
    else:
        table = _BITRATES_MPEG2_L1 if layer == 1 else _BITRATES_MPEG2_L23
    bitrate = table[br_index] * 1000
    sample_rate = _SAMPLE_RATES[version][(word >> 10) & 0x3]
    mode = (word >> 6) & 0x3
    mono = mode == 0b11
    if layer == 2:
        if mono and bitrate in (224_000, 256_000, 320_000, 384_000):
            return None
        if not mono and bitrate in (32_000, 48_000, 56_000, 80_000):
            return None
    factor = 12 if layer == 1 else (144 if layer == 2 or version == 3 else 72)
    slot = 4 if layer == 1 else 1
    slots = factor * bitrate // sample_rate + (1 if word & 0x200 else 0)
    return _Header(
        version=version,
        layer=layer,
        has_crc=word & 0x1_0000 == 0,
        sample_rate=sample_rate,
        mono=mono,
        frame_len=slots * slot,
    )


def _similar(a: _Header, b: _Header) -> bool:
    return (a.version, a.layer, a.sample_rate, a.mono) == (b.version, b.layer, b.sample_rate, b.mono)


def _id3v2_len(head: bytes) -> int:
    """Length of the ID3v2 tag (footer included) starting ``head``, or 0 when none."""
    if len(head) < 10 or head[:3] != b"ID3" or any(b & 0x80 for b in head[6:10]):
        return 0
    size = 0
    for b in head[6:10]:
        size = (size << 7) | b
    return 10 + size + (10 if head[5] & 0x10 else 0)


def _first_frame(buf: bytes, start: int) -> tuple[_Header, int] | None:
    """symphonia's ``read_mpeg_frame_strict``: the first frame a similar frame follows."""
    pos = start
    end = min(len(buf), start + _MAX_SYNC_SCAN)
    while pos + _HEADER_LEN <= end:
        if buf[pos] != 0xFF or buf[pos + 1] & 0xE0 != 0xE0:
            pos += 1
            continue
        header = _parse_header(int.from_bytes(buf[pos : pos + 4], "big"))
        if header is None or header.frame_len <= _HEADER_LEN:
            pos += 1
            continue
        nxt = pos + header.frame_len
        if nxt + _HEADER_LEN <= len(buf):
            following = _parse_header(int.from_bytes(buf[nxt : nxt + 4], "big"))
            if following is None or not _similar(header, following):
                pos += 1
                continue
        if nxt > len(buf):
            return None
        return header, pos
    return None


def _crc16_arc(data: bytes, crc: int = 0) -> int:
    """CRC-16/ARC (reflected 0x8005, init 0): symphonia's ``Crc16AnsiLe``, LAME's tag CRC."""
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _lame_delay(frame: bytes, header: _Header) -> tuple[int, str | None]:
    """Trimmed frames and encoder name from the first frame's Xing/Info tag (0 when none)."""
    if header.layer != 3:
        return 0, None
    offset = _HEADER_LEN + header.side_info_len
    if len(frame) < offset + 8:
        return 0, None
    if frame[offset : offset + 4] not in (b"Xing", b"Info"):
        return 0, None
    if any(frame[header.header_size : offset]):
        return 0, None
    pos = offset + 4
    flags = int.from_bytes(frame[pos : pos + 4], "big")
    pos += 4
    pos += 4 * bool(flags & 0x1) + 4 * bool(flags & 0x2) + 100 * bool(flags & 0x4)
    pos += 4 * bool(flags & 0x8)
    if pos > len(frame) or len(frame) - pos < _MIN_LAME_EXT_LEN:
        return 0, None
    encoder = frame[pos : pos + 9]
    trim = int.from_bytes(frame[pos + 21 : pos + 24], "big")
    known = encoder[:4] in _LAME_ENCODERS
    delay = DECODER_DELAY + (trim >> 12) if known else 0
    after = pos + _MIN_LAME_EXT_LEN
    if len(frame) - after >= _LAME_EXT_LEN - _MIN_LAME_EXT_LEN and (
        header.has_crc or encoder[:4] == b"LAME"
    ):
        crc_at = after + 10
        stored = int.from_bytes(frame[crc_at : crc_at + 2], "big")
        if stored != 0 and stored != _crc16_arc(frame[:crc_at]):
            return 0, None  # not a LAME tag after all: symphonia ignores it
    name = encoder.split(b"\0", 1)[0].decode("latin-1").strip() or None
    return delay, name


def read_lead_in(path: Path) -> LeadIn:
    """The lead-in symphonia trims from ``path``. Raises OSError when it cannot be read.

    A file that is not ``.mp3`` trims nothing here (``NO_LEAD_IN``): AAC
    priming is a different mechanism with different engine behavior, and out
    of this module's scope.
    """
    if path.suffix.lower() != ".mp3":
        return NO_LEAD_IN
    with path.open("rb") as fh:
        start = 0
        # Stacked ID3v2 tags each state their own size; the probe skips them all.
        while tag_len := _id3v2_len(fh.read(10)):
            start += tag_len
            fh.seek(start)
        fh.seek(start)
        buf = fh.read(_MAX_SYNC_SCAN + 2 * 4096)
    found = _first_frame(buf, 0)
    if found is None:
        return NO_LEAD_IN
    header, at = found
    frames, encoder = _lame_delay(buf[at : at + header.frame_len], header)
    return LeadIn(frames=frames, sample_rate=header.sample_rate, encoder=encoder)


@functools.lru_cache(maxsize=16_384)
def _cached_seconds(path: str, _mtime_ns: int, _size: int) -> float:
    return read_lead_in(Path(path)).seconds


def lead_in_seconds(path: Path) -> float:
    """``read_lead_in(path).seconds``, memoized on the file's identity. Raises OSError."""
    st = os.stat(path)
    return _cached_seconds(str(path), st.st_mtime_ns, st.st_size)


def rekordbox_lead_in_s(folder_path: str | None) -> float | None:
    """Seconds between rekordbox's time zero and ours for one ``FolderPath``.

    None when there is no local, materialized file to read: a streaming row,
    an unmapped path, an iCloud stub. Such a track cannot play here either, so
    a reader that serves positions may treat None as 0. A writer that sends
    positions back to rekordbox must not, because it would write them off.
    """
    if not folder_path or platform_paths.is_streaming_uri(folder_path):
        return None
    mapped = platform_paths.resolve_asset_path(folder_path)
    path = mapped.resolved
    if path is None or not fs_residency.is_materialised(path):
        return None
    try:
        return lead_in_seconds(path)
    except OSError:
        return None


def to_our_ms(rekordbox_ms: int, lead_in_s: float) -> int:
    """A rekordbox position in whole ms on our timeline, floored at 0."""
    return max(0, round(rekordbox_ms - lead_in_s * 1000))


def to_our_s(rekordbox_s: float, lead_in_s: float) -> float:
    """A rekordbox position in seconds on our timeline (may go below 0)."""
    return rekordbox_s - lead_in_s


def to_rekordbox_s(our_s: float, lead_in_s: float) -> float:
    """A position on our timeline in rekordbox's seconds."""
    return our_s + lead_in_s


__all__ = [
    "DECODER_DELAY",
    "NO_LEAD_IN",
    "LeadIn",
    "lead_in_seconds",
    "read_lead_in",
    "rekordbox_lead_in_s",
    "to_our_ms",
    "to_our_s",
    "to_rekordbox_s",
]
