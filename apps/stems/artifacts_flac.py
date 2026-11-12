"""FLAC stem container metadata (STREAMINFO and tail-frame validation).

Supersedes: apps/stems/flac_metadata.py (issue #11223 / #1390 paydown name).
"""

from __future__ import annotations

import struct
from pathlib import Path

from apps.stems.artifact_types import StemArtifactError, WavMetadata
from apps.stems.flac_frame import (
    _flac_block_size_from_header,
    _flac_frame_crc16_valid,
    _flac_stream_end,
    _flac_valid_frame_header_at,
)

__all__ = [
    "_flac_block_size_from_header",
    "_flac_valid_frame_header_at",
    "_require_flac_magic",
    "read_flac_metadata",
]


def _require_flac_magic(path: Path) -> None:
    try:
        with path.open("rb") as source:
            magic = source.read(4)
    except OSError as exc:
        raise StemArtifactError(f"cannot read FLAC header for {path.name}: {exc}") from exc
    if magic != b"fLaC":
        raise StemArtifactError(f"{path.name} is not a FLAC file (missing fLaC magic)")


def read_flac_metadata(path: Path) -> WavMetadata:
    """Parse FLAC STREAMINFO without decoding audio."""
    try:
        with path.open("rb") as source:
            magic = source.read(4)
            if magic != b"fLaC":
                raise StemArtifactError(f"{path.name} is not a FLAC file (missing fLaC magic)")
            streaminfo: bytes | None = None
            while True:
                header = source.read(4)
                if len(header) != 4:
                    raise StemArtifactError(f"{path.name} has truncated FLAC metadata")
                block_header = struct.unpack(">I", header)[0]
                is_last = (block_header & 0x80000000) != 0
                block_type = (block_header & 0x7F000000) >> 24
                block_len = block_header & 0x00FFFFFF
                payload = source.read(block_len)
                if len(payload) != block_len:
                    raise StemArtifactError(f"{path.name} has truncated FLAC metadata block")
                if block_type == 0:
                    streaminfo = payload
                if is_last:
                    break
            if streaminfo is None:
                raise StemArtifactError(f"{path.name} FLAC missing STREAMINFO block")
            metadata = _parse_flac_streaminfo(path.name, streaminfo)
            _flac_tail_frame_covers_stream(path, streaminfo=streaminfo)
            return metadata
    except OSError as exc:
        raise StemArtifactError(f"cannot read FLAC metadata for {path.name}: {exc}") from exc


def _parse_flac_streaminfo(file_name: str, streaminfo: bytes) -> WavMetadata:
    if len(streaminfo) < 18:
        raise StemArtifactError(f"{file_name} STREAMINFO block is too short")
    packed = int.from_bytes(streaminfo[10:18], "big")
    sample_rate = (packed >> 44) & 0xFFFFF
    channels = ((packed >> 41) & 0x7) + 1
    frame_count = packed & 0xFFFFFFFFF
    if sample_rate <= 0 or channels <= 0 or frame_count <= 0:
        raise StemArtifactError(f"{file_name} FLAC STREAMINFO has invalid geometry")
    return WavMetadata(
        sample_rate=sample_rate,
        frame_count=frame_count,
        channels=channels,
    )


def _flac_read_tail(path: Path) -> tuple[bytes, int]:
    try:
        file_size = path.stat().st_size
        tail_len = min(file_size, 65_536)
        with path.open("rb") as handle:
            handle.seek(file_size - tail_len)
            tail = handle.read(tail_len)
        stream_end = _flac_stream_end(tail)
    except OSError as exc:
        raise StemArtifactError(f"cannot read FLAC tail for {path.name}: {exc}") from exc
    return tail, stream_end


def _flac_tail_scan_exact_final(
    tail: bytes,
    stream_end: int,
    *,
    min_blocksize: int,
    max_blocksize: int,
    total_samples: int,
) -> tuple[int | None, bool]:
    """Return (best_end_sample_from_last_valid_header, found_exact_match_with_crc)."""
    best_end: int | None = None
    found_exact = False
    for index in range(len(tail) - 1):
        if tail[index] != 0xFF or tail[index + 1] not in {0xF8, 0xF9}:
            continue
        validated = _flac_valid_frame_header_at(
            tail,
            index,
            min_blocksize=min_blocksize,
            max_blocksize=max_blocksize,
        )
        if validated is None:
            continue
        best_end = validated[0]
        if best_end != total_samples:
            continue
        if not _flac_frame_crc16_valid(tail, index, stream_end):
            continue
        found_exact = True
        break
    return best_end, found_exact


def _flac_tail_frame_covers_stream(path: Path, *, streaminfo: bytes) -> None:
    """Require the file tail contains a genuine frame reaching STREAMINFO total_samples."""
    if len(streaminfo) < 18:
        raise StemArtifactError(f"{path.name} STREAMINFO block is too short")
    min_blocksize = struct.unpack(">H", streaminfo[0:2])[0]
    max_blocksize = struct.unpack(">H", streaminfo[2:4])[0]
    packed = int.from_bytes(streaminfo[10:18], "big")
    total_samples = packed & 0xFFFFFFFFF
    if min_blocksize <= 0 or max_blocksize <= 0 or total_samples <= 0:
        raise StemArtifactError(f"{path.name} FLAC STREAMINFO has invalid geometry")

    tail, stream_end = _flac_read_tail(path)
    best_end, found_exact = _flac_tail_scan_exact_final(
        tail,
        stream_end,
        min_blocksize=min_blocksize,
        max_blocksize=max_blocksize,
        total_samples=total_samples,
    )
    if not found_exact:
        found = "no valid frame header found in file tail"
        if best_end is not None:
            found = f"last tail frame candidate ends at sample {best_end} but final-frame CRC-16 footer check failed"
        raise StemArtifactError(
            f"{path.name} FLAC tail frame check failed: STREAMINFO declares "
            f"{total_samples} total samples but {found} "
            f"(need a frame whose own decoded end sample equals {total_samples} "
            "exactly with a valid CRC-16 footer)"
        )
