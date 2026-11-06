"""RIFF/WAVE stem container metadata."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from apps.stems.artifact_types import StemArtifactError, WavMetadata


@dataclass(frozen=True, slots=True)
class _WavChunkApplyCtx:
    source: object
    file_name: str
    payload_start: int
    payload_end: int
    file_size: int


def read_wav_metadata(path: Path) -> WavMetadata:
    """Read RIFF/WAVE metadata without decoding or altering the audio samples."""
    try:
        file_size = path.stat().st_size
        with path.open("rb") as source:
            header = source.read(12)
            if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
                raise StemArtifactError(f"{path.name} is not a RIFF/WAVE file")
            riff_size = struct.unpack("<I", header[4:8])[0]
            if riff_size + 8 != file_size:
                raise StemArtifactError(f"{path.name} RIFF size does not match its actual file size")
            return _parse_riff_wav_chunks(source, file_size=file_size, file_name=path.name)
    except OSError as exc:
        raise StemArtifactError(f"cannot read WAV metadata for {path.name}: {exc}") from exc


def _read_wav_fmt_payload(source, *, file_name: str, payload_start: int, chunk_size: int) -> tuple[int, int, int, int]:
    if chunk_size < 16:
        raise StemArtifactError(f"{file_name} has an invalid fmt chunk")
    source.seek(payload_start)
    audio_format, channels, sample_rate, _byte_rate, block_align, _bits = struct.unpack(
        "<HHIIHH", source.read(16)
    )
    if audio_format not in {1, 3, 0xFFFE}:
        raise StemArtifactError(f"{file_name} uses unsupported WAV format {audio_format}")
    return channels, sample_rate, block_align, audio_format


def _wav_apply_chunk(
    chunk_id: bytes,
    chunk_size: int,
    *,
    ctx: _WavChunkApplyCtx,
    fmt: tuple[int, int, int, int] | None,
    data_size: int,
) -> tuple[tuple[int, int, int, int] | None, int]:
    if ctx.payload_end > ctx.file_size:
        raise StemArtifactError(f"{ctx.file_name} has a RIFF chunk beyond EOF")
    if chunk_id == b"fmt ":
        if fmt is not None:
            raise StemArtifactError(f"{ctx.file_name} contains more than one fmt chunk")
        fmt = _read_wav_fmt_payload(
            ctx.source, file_name=ctx.file_name, payload_start=ctx.payload_start, chunk_size=chunk_size
        )
    elif chunk_id == b"data":
        data_size += chunk_size
    return fmt, data_size


def _parse_riff_wav_chunks(source, *, file_size: int, file_name: str) -> WavMetadata:
    fmt: tuple[int, int, int, int] | None = None
    data_size = 0
    offset = 12
    while offset < file_size:
        source.seek(offset)
        chunk_header = source.read(8)
        if len(chunk_header) != 8:
            raise StemArtifactError(f"{file_name} has a truncated RIFF chunk header")
        chunk_id, chunk_size = (
            chunk_header[:4],
            struct.unpack("<I", chunk_header[4:])[0],
        )
        payload_start = offset + 8
        payload_end = payload_start + chunk_size
        fmt, data_size = _wav_apply_chunk(
            chunk_id,
            chunk_size,
            ctx=_WavChunkApplyCtx(
                source=source,
                file_name=file_name,
                payload_start=payload_start,
                payload_end=payload_end,
                file_size=file_size,
            ),
            fmt=fmt,
            data_size=data_size,
        )
        offset = payload_end + (chunk_size % 2)

    if fmt is None or data_size == 0:
        raise StemArtifactError(f"{file_name} must contain fmt and non-empty data chunks")
    channels, sample_rate, block_align, _audio_format = fmt
    if channels <= 0 or sample_rate <= 0 or block_align <= 0 or data_size % block_align:
        raise StemArtifactError(f"{file_name} contains inconsistent WAV metadata")
    return WavMetadata(
        sample_rate=sample_rate,
        frame_count=data_size // block_align,
        channels=channels,
    )
