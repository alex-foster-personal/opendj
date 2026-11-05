"""Test-only RIFF/FORM writers for WAV/AIFF structural probe cases.

WAVEFORMATEXTENSIBLE bytes are synthesized from the MSDN layout
(KSDATAFORMAT_SUBTYPE_PCM / IEEE_FLOAT), independent of the parser.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

# MSDN GUID layout on disk (little-endian first three fields).
GUID_PCM = bytes.fromhex("0100000000001000800000aa00389b71")
GUID_IEEE_FLOAT = bytes.fromhex("0300000000001000800000aa00389b71")

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
WAVE_FORMAT_ADPCM = 0x0002
WAVE_FORMAT_MP3 = 0x0055

_RATE = 44100
_FRAMES = 4410  # 0.1 s, so the duration/size check's bitrate path runs


def _pad_chunk(payload: bytes) -> bytes:
    return payload + (b"\x00" if len(payload) % 2 else b"")


def _riff(chunks: list[tuple[bytes, bytes]], *, riff_size: int | None = None) -> bytes:
    body = b"".join(cid + struct.pack("<I", len(payload)) + _pad_chunk(payload) for cid, payload in chunks)
    size = 4 + len(body) if riff_size is None else riff_size
    return b"RIFF" + struct.pack("<I", size) + b"WAVE" + body


def _form(
    form_type: bytes,
    chunks: list[tuple[bytes, bytes]],
    *,
    form_size: int | None = None,
) -> bytes:
    body = b"".join(cid + struct.pack(">I", len(payload)) + _pad_chunk(payload) for cid, payload in chunks)
    size = 4 + len(body) if form_size is None else form_size
    return b"FORM" + struct.pack(">I", size) + form_type + body


def pcm_fmt(*, channels: int = 1, rate: int = _RATE, bits: int = 16, extra: bytes = b"") -> bytes:
    width = bits // 8
    block = channels * width
    return struct.pack("<HHIIHH", WAVE_FORMAT_PCM, channels, rate, rate * block, block, bits) + extra


def float_fmt(*, channels: int = 1, rate: int = _RATE, bits: int = 32, extra: bytes = b"") -> bytes:
    width = bits // 8
    block = channels * width
    return struct.pack("<HHIIHH", WAVE_FORMAT_IEEE_FLOAT, channels, rate, rate * block, block, bits) + extra


def extensible_fmt(
    *,
    channels: int = 1,
    rate: int = _RATE,
    bits: int = 16,
    valid_bits: int | None = None,
    mask: int = 0,
    guid: bytes = GUID_PCM,
    cb_size: int = 22,
    extra: bytes = b"",
) -> bytes:
    """40-byte WAVEFORMATEXTENSIBLE fmt payload (MSDN layout)."""
    width = bits // 8
    block = channels * width
    valid = bits if valid_bits is None else valid_bits
    return (
        struct.pack("<HHIIHH", WAVE_FORMAT_EXTENSIBLE, channels, rate, rate * block, block, bits)
        + struct.pack("<HHI", cb_size, valid, mask)
        + guid
        + extra
    )


def tagged_fmt(tag: int, *, channels: int = 1, rate: int = _RATE, bits: int = 16) -> bytes:
    width = max(bits // 8, 1)
    block = channels * width
    return struct.pack("<HHIIHH", tag, channels, rate, rate * block, block, bits)


def pcm_data(*, frames: int = _FRAMES, channels: int = 1, bits: int = 16) -> bytes:
    return b"\x00" * (frames * channels * (bits // 8))


def write_wav(path: Path, chunks: list[tuple[bytes, bytes]], *, riff_size: int | None = None) -> Path:
    path.write_bytes(_riff(chunks, riff_size=riff_size))
    return path


def write_pcm_wav(path: Path, *, frames: int = _FRAMES) -> Path:
    data = pcm_data(frames=frames)
    return write_wav(path, [(b"fmt ", pcm_fmt()), (b"data", data)])


def write_float16_wav(path: Path, *, frames: int = _FRAMES) -> Path:
    """IEEE-float wav with a 16-byte fmt (libsndfile/soundfile layout)."""
    data = pcm_data(frames=frames, bits=32)
    return write_wav(path, [(b"fmt ", float_fmt()), (b"data", data)])


def write_extensible_wav(
    path: Path,
    *,
    guid: bytes = GUID_PCM,
    bits: int = 16,
    valid_bits: int | None = None,
    mask: int = 0,
    frames: int = _FRAMES,
    cb_size: int = 22,
    fmt_extra: bytes = b"",
) -> Path:
    data = pcm_data(frames=frames, bits=bits)
    fmt = extensible_fmt(bits=bits, valid_bits=valid_bits, mask=mask, guid=guid, cb_size=cb_size, extra=fmt_extra)
    return write_wav(path, [(b"fmt ", fmt), (b"data", data)])


def write_tagged_wav(path: Path, tag: int, *, frames: int = _FRAMES) -> Path:
    data = pcm_data(frames=frames)
    return write_wav(path, [(b"fmt ", tagged_fmt(tag)), (b"data", data)])


def write_wav_with_fact(
    path: Path,
    *,
    fact_payload: bytes,
    frames: int = _FRAMES,
) -> Path:
    data = pcm_data(frames=frames)
    return write_wav(path, [(b"fmt ", pcm_fmt()), (b"fact", fact_payload), (b"data", data)])


def write_wav_missing_data(path: Path) -> Path:
    return write_wav(path, [(b"fmt ", pcm_fmt())])


def write_wav_missing_fmt(path: Path, *, frames: int = _FRAMES) -> Path:
    return write_wav(path, [(b"data", pcm_data(frames=frames))])


def write_wav_post_data_truncated_header(path: Path, *, frames: int = _FRAMES) -> Path:
    data = pcm_data(frames=frames)
    chunks = [(b"fmt ", pcm_fmt()), (b"data", data)]
    raw = _riff(chunks)
    raw = raw + b"JUNK"
    riff_size = len(raw) - 8
    path.write_bytes(b"RIFF" + struct.pack("<I", riff_size) + raw[8:])
    return path


def encode_ieee80(rate: float) -> bytes:
    """Pack a Python float as an 80-bit SANE extended value (AIFF sample rate)."""
    if rate == 0.0:
        return bytes(10)
    sign = 0x8000 if math.copysign(1.0, rate) < 0 else 0
    mantissa, exp = math.frexp(abs(rate))
    mantissa *= 2.0
    exp -= 1
    exponent = exp + 16383
    frac = int(mantissa * (1 << 63))
    return struct.pack(">HQ", sign | exponent, frac)


def _comm_core(*, channels: int, frames: int, sample_size: int, rate: float = float(_RATE)) -> bytes:
    return struct.pack(">hIH", channels, frames, sample_size) + encode_ieee80(rate)


def _pascal(name: bytes) -> bytes:
    blob = bytes([len(name)]) + name
    if len(blob) % 2:
        blob += b"\x00"
    return blob


def aiff_comm(*, channels: int = 1, frames: int = _FRAMES, sample_size: int = 16) -> bytes:
    return _comm_core(channels=channels, frames=frames, sample_size=sample_size)


def aifc_comm(
    fourcc: bytes,
    *,
    channels: int = 1,
    frames: int = _FRAMES,
    sample_size: int = 16,
    name: bytes = b"",
    include_name: bool = True,
) -> bytes:
    core = _comm_core(channels=channels, frames=frames, sample_size=sample_size) + fourcc
    if not include_name:
        return core
    return core + _pascal(name)


def ssnd_payload(*, frames: int = _FRAMES, channels: int = 1, sample_size: int = 16) -> bytes:
    width = (sample_size + 7) // 8
    return struct.pack(">II", 0, 0) + b"\x00" * (frames * channels * width)


def write_aiff(path: Path, *, frames: int = _FRAMES) -> Path:
    chunks = [(b"COMM", aiff_comm(frames=frames)), (b"SSND", ssnd_payload(frames=frames))]
    path.write_bytes(_form(b"AIFF", chunks))
    return path


def write_aifc(
    path: Path,
    fourcc: bytes,
    *,
    frames: int = _FRAMES,
    sample_size: int = 16,
    name: bytes = b"",
    include_ssnd: bool = True,
    include_name: bool = True,
    comm_payload: bytes | None = None,
) -> Path:
    comm = (
        comm_payload
        if comm_payload is not None
        else aifc_comm(fourcc, frames=frames, sample_size=sample_size, name=name, include_name=include_name)
    )
    chunks: list[tuple[bytes, bytes]] = [(b"COMM", comm)]
    if include_ssnd:
        chunks.append((b"SSND", ssnd_payload(frames=frames, sample_size=sample_size)))
    path.write_bytes(_form(b"AIFC", chunks))
    return path


def write_aiff_post_ssnd_truncated_header(path: Path, *, frames: int = _FRAMES) -> Path:
    chunks = [(b"COMM", aiff_comm(frames=frames)), (b"SSND", ssnd_payload(frames=frames))]
    raw = _form(b"AIFF", chunks)
    raw = raw + b"JUNK"
    form_size = len(raw) - 8
    path.write_bytes(b"FORM" + struct.pack(">I", form_size) + raw[8:])
    return path


def write_form_type(path: Path, form_type: bytes) -> Path:
    chunks = [(b"COMM", aiff_comm()), (b"SSND", ssnd_payload())]
    path.write_bytes(_form(form_type, chunks))
    return path
