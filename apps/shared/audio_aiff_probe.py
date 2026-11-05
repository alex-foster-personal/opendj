"""FORM/AIFF/AIFC container walk for folder-ingest playability (SETUP-16 / #916)."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from apps.shared.audio_chunk_io import UnplayableAudioError, read_chunk

_ACCEPTED_FOURCC: dict[bytes, frozenset[int]] = {
    b"NONE": frozenset({8, 16, 24, 32}),
    b"sowt": frozenset({8, 16, 24, 32}),
    b"twos": frozenset({8, 16, 24, 32}),
    b"in24": frozenset({24}),
    b"in32": frozenset({32}),
    b"fl32": frozenset({32}),
    b"fl64": frozenset({64}),
}
_UNSUPPORTED_FOURCC = frozenset({b"ima4", b"mac3", b"mac6", b"ulaw", b"alaw", b"ULAW", b"ALAW"})
_AIFF_BITS = frozenset({8, 16, 24, 32})


@dataclass(frozen=True)
class _Comm:
    channels: int
    frames: int
    sample_size: int
    rate: float


def probe_aiff(path: Path) -> tuple[int, int, int]:
    """Walk an AIFF/AIFC file. Return ``(rate, channels, frames)``."""
    file_size = path.stat().st_size
    with path.open("rb") as handle:
        return _probe_open_aiff(handle, file_size)


def _probe_open_aiff(handle: BinaryIO, file_size: int) -> tuple[int, int, int]:
    header = handle.read(12)
    if len(header) < 12 or header[:4] != b"FORM":
        raise UnplayableAudioError("missing aiff form header")
    form_type = header[8:12]
    if form_type not in {b"AIFF", b"AIFC"}:
        raise UnplayableAudioError("missing aiff form type")
    form_size = struct.unpack_from(">I", header, 4)[0]
    if 8 + form_size > file_size:
        raise UnplayableAudioError("truncated form")
    container_end = min(file_size, 8 + form_size)
    is_aifc = form_type == b"AIFC"
    comm, ssnd_size = _walk_aiff(handle, file_size, container_end, is_aifc)
    _check_ssnd_payload(comm, ssnd_size)
    return int(comm.rate), comm.channels, comm.frames


def _walk_aiff(handle: BinaryIO, file_size: int, container_end: int, is_aifc: bool) -> tuple[_Comm, int]:
    comm: _Comm | None = None
    ssnd_size: int | None = None
    offset = 12
    while offset < container_end:
        chunk_id, payload_start, payload_size, next_offset = read_chunk(handle, offset, file_size, container_end, "big")
        if chunk_id == b"COMM":
            if comm is not None:
                raise UnplayableAudioError("duplicate COMM chunk")
            comm = _parse_comm(handle, payload_start, payload_size, is_aifc)
        elif chunk_id == b"SSND":
            if ssnd_size is not None:
                raise UnplayableAudioError("duplicate SSND chunk")
            if payload_size < 8:
                raise UnplayableAudioError("truncated SSND chunk")
            ssnd_size = payload_size
        offset = next_offset
    if comm is None:
        raise UnplayableAudioError("missing COMM chunk")
    if ssnd_size is None:
        raise UnplayableAudioError("missing SSND chunk")
    return comm, ssnd_size


def _parse_comm(handle: BinaryIO, start: int, size: int, is_aifc: bool) -> _Comm:
    minimum = 22 if is_aifc else 18
    if size < minimum:
        raise UnplayableAudioError("truncated COMM chunk")
    handle.seek(start)
    payload = handle.read(size)
    if len(payload) < size:
        raise UnplayableAudioError("truncated COMM chunk")
    channels, frames, sample_size = struct.unpack_from(">hIH", payload, 0)
    rate = _decode_sane_extended(payload[8:18])
    if not math.isfinite(rate) or rate < 8000 or rate > 192_000:
        raise UnplayableAudioError("implausible sample rate")
    if channels < 1 or channels > 8:
        raise UnplayableAudioError("implausible channel count")
    if frames <= 0:
        raise UnplayableAudioError("no pcm frames")
    allowed = _aifc_bits(payload) if is_aifc else _AIFF_BITS
    if sample_size not in allowed:
        raise UnplayableAudioError("implausible sample width")
    return _Comm(channels, frames, sample_size, rate)


def _aifc_bits(payload: bytes) -> frozenset[int]:
    fourcc = payload[18:22]
    _check_pascal_name(payload, 22)
    if fourcc in _UNSUPPORTED_FOURCC:
        raise UnplayableAudioError(f"unsupported aifc compression {fourcc.decode('latin-1')}")
    allowed = _ACCEPTED_FOURCC.get(fourcc)
    if allowed is None:
        raise UnplayableAudioError(f"unknown aifc compression {fourcc!r}")
    return allowed


def _check_pascal_name(payload: bytes, offset: int) -> None:
    if offset >= len(payload):
        raise UnplayableAudioError("truncated aifc compression name")
    n = payload[offset]
    end = offset + 1 + n
    if end > len(payload):
        raise UnplayableAudioError("truncated aifc compression name")
    if (1 + n) % 2 and end + 1 > len(payload):
        raise UnplayableAudioError("truncated aifc compression name")


def _decode_sane_extended(data: bytes) -> float:
    if len(data) < 10:
        raise UnplayableAudioError("truncated aiff sample rate")
    exp_and_sign = int.from_bytes(data[:2], "big")
    mantissa = int.from_bytes(data[2:10], "big")
    sign = -1.0 if exp_and_sign & 0x8000 else 1.0
    exponent = exp_and_sign & 0x7FFF
    if exponent == 0 and mantissa == 0:
        return 0.0
    if exponent == 0x7FFF:
        return sign * float("inf") if mantissa == 0 else float("nan")
    return sign * float(mantissa) * (2.0 ** (exponent - 16383 - 63))


def _check_ssnd_payload(comm: _Comm, ssnd_size: int) -> None:
    width = (comm.sample_size + 7) // 8
    expected = comm.frames * comm.channels * width
    sample_bytes = ssnd_size - 8
    if sample_bytes != expected:
        raise UnplayableAudioError("inconsistent SSND sample size")
