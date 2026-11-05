"""RIFF/WAVE container walk for folder-ingest playability (SETUP-16 / #916)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from apps.shared.audio_chunk_io import UnplayableAudioError, read_chunk

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

# KSDATAFORMAT_SUBTYPE_PCM  {00000001-0000-0010-8000-00aa00389b71}
GUID_PCM = bytes.fromhex("0100000000001000800000aa00389b71")
# KSDATAFORMAT_SUBTYPE_IEEE_FLOAT {00000003-0000-0010-8000-00aa00389b71}
GUID_IEEE_FLOAT = bytes.fromhex("0300000000001000800000aa00389b71")

_PCM_BITS = frozenset({8, 16, 24, 32})
_FLOAT_BITS = frozenset({32, 64})
_STREAM_SIZES = frozenset({0, 0xFFFFFFFF})


@dataclass(frozen=True)
class _Fmt:
    channels: int
    rate: int
    block_align: int


def probe_wav(path: Path) -> tuple[int, int, int]:
    """Walk a WAVE file. Return ``(rate, channels, frames)``."""
    file_size = path.stat().st_size
    with path.open("rb") as handle:
        return _probe_open_wav(handle, file_size)


def _probe_open_wav(handle: BinaryIO, file_size: int) -> tuple[int, int, int]:
    header = handle.read(12)
    if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise UnplayableAudioError("missing wav riff header")
    riff_size = struct.unpack_from("<I", header, 4)[0]
    streaming = riff_size in _STREAM_SIZES
    if not streaming and 8 + riff_size > file_size:
        raise UnplayableAudioError("truncated riff")
    container_end = file_size if streaming else min(file_size, 8 + riff_size)
    fmt, data_size = _walk_wav(handle, file_size, container_end)
    frames = _frames_from_data(data_size, fmt.block_align, streaming)
    return fmt.rate, fmt.channels, frames


def _walk_wav(handle: BinaryIO, file_size: int, container_end: int) -> tuple[_Fmt, int]:
    fmt: _Fmt | None = None
    data_size: int | None = None
    saw_fact = False
    offset = 12
    while offset < container_end:
        chunk_id, payload_start, payload_size, next_offset = read_chunk(
            handle, offset, file_size, container_end, "little"
        )
        if chunk_id == b"fmt ":
            if fmt is not None:
                raise UnplayableAudioError("duplicate fmt chunk")
            fmt = _parse_fmt(handle, payload_start, payload_size)
        elif chunk_id == b"data":
            if data_size is not None:
                raise UnplayableAudioError("duplicate data chunk")
            if payload_size <= 0:
                raise UnplayableAudioError("empty data chunk")
            data_size = payload_size
        elif chunk_id == b"fact":
            if saw_fact:
                raise UnplayableAudioError("duplicate fact chunk")
            saw_fact = True
            _parse_fact(handle, payload_start, payload_size)
        offset = next_offset
    if fmt is None:
        raise UnplayableAudioError("missing fmt chunk")
    if data_size is None:
        raise UnplayableAudioError("missing data chunk")
    return fmt, data_size


def _parse_fmt(handle: BinaryIO, start: int, size: int) -> _Fmt:
    if size < 16:
        raise UnplayableAudioError("truncated fmt chunk")
    handle.seek(start)
    payload = handle.read(size)
    if len(payload) < size:
        raise UnplayableAudioError("truncated fmt chunk")
    tag, channels, rate, avg_bytes, block_align, bits = struct.unpack_from("<HHIIHH", payload, 0)
    if tag == WAVE_FORMAT_PCM:
        _check_pcm_like(channels, rate, avg_bytes, block_align, bits, _PCM_BITS)
        return _Fmt(channels, rate, block_align)
    if tag == WAVE_FORMAT_IEEE_FLOAT:
        _check_float_cb_size(payload)
        _check_pcm_like(channels, rate, avg_bytes, block_align, bits, _FLOAT_BITS)
        return _Fmt(channels, rate, block_align)
    if tag == WAVE_FORMAT_EXTENSIBLE:
        return _parse_extensible(payload, channels, rate, avg_bytes, block_align, bits)
    raise UnplayableAudioError(f"unsupported wav format 0x{tag:04x}")


def _check_float_cb_size(payload: bytes) -> None:
    if len(payload) < 18:
        return
    cb_size = struct.unpack_from("<H", payload, 16)[0]
    if 18 + cb_size > len(payload):
        raise UnplayableAudioError("float cbSize overruns fmt")


def _parse_extensible(
    payload: bytes,
    channels: int,
    rate: int,
    avg_bytes: int,
    block_align: int,
    bits: int,
) -> _Fmt:
    if len(payload) < 40:
        raise UnplayableAudioError("truncated extensible fmt")
    cb_size = struct.unpack_from("<H", payload, 16)[0]
    if cb_size < 22:
        raise UnplayableAudioError("extensible cbSize too small")
    if 18 + cb_size > len(payload):
        raise UnplayableAudioError("extensible cbSize overruns fmt")
    valid_bits, mask = struct.unpack_from("<HI", payload, 18)
    guid = payload[24:40]
    allowed = _guid_bits(guid)
    if valid_bits != 0 and valid_bits > bits:
        raise UnplayableAudioError("extensible valid bits exceed container bits")
    if mask != 0 and mask.bit_count() != channels:
        raise UnplayableAudioError("extensible channel mask popcount mismatch")
    _check_pcm_like(channels, rate, avg_bytes, block_align, bits, allowed)
    return _Fmt(channels, rate, block_align)


def _guid_bits(guid: bytes) -> frozenset[int]:
    if guid == GUID_PCM:
        return _PCM_BITS
    if guid == GUID_IEEE_FLOAT:
        return _FLOAT_BITS
    raise UnplayableAudioError("extensible subformat is not PCM or IEEE-float")


def _check_pcm_like(
    channels: int,
    rate: int,
    avg_bytes: int,
    block_align: int,
    bits: int,
    allowed_bits: frozenset[int],
) -> None:
    if rate < 8000 or rate > 192_000:
        raise UnplayableAudioError("implausible sample rate")
    if channels < 1 or channels > 8:
        raise UnplayableAudioError("implausible channel count")
    if bits not in allowed_bits or bits % 8 != 0:
        raise UnplayableAudioError("implausible sample width")
    expected_block = channels * (bits // 8)
    if block_align != expected_block:
        raise UnplayableAudioError("inconsistent block align")
    if avg_bytes != rate * block_align:
        raise UnplayableAudioError("inconsistent byte rate")


def _parse_fact(handle: BinaryIO, start: int, size: int) -> None:
    if size < 4:
        raise UnplayableAudioError("truncated fact chunk")
    handle.seek(start)
    raw = handle.read(4)
    if len(raw) < 4:
        raise UnplayableAudioError("truncated fact chunk")
    length = struct.unpack("<I", raw)[0]
    if length == 0:
        raise UnplayableAudioError("fact sample length is zero")


def _frames_from_data(data_size: int, block_align: int, streaming: bool) -> int:
    if block_align <= 0:
        raise UnplayableAudioError("implausible block align")
    frames = data_size // block_align
    if frames <= 0:
        raise UnplayableAudioError("no pcm frames")
    if streaming:
        return frames
    if data_size % block_align != 0:
        raise UnplayableAudioError("data size is not a multiple of block align")
    return frames
