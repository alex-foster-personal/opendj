"""MP3 stem container metadata (Layer III frame geometry without decode)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from apps.stems.artifact_types import StemArtifactError, WavMetadata
from apps.stems.mp3_frame import (
    _find_mp3_sync,
    _id3v2_skip_size,
    _looks_like_mp3,
    _mp3_side_info_len,
    _mpeg_bitrate_kbps,
    _mpeg_layer3_frame_size,
    _mpeg_sample_rate,
    _parse_mpeg_frame_header,
    _read_mp3_probe,
)
from apps.stems.mp3_scan import scan_mp3_frames
from apps.stems.mp3_xing import (
    _MP3XingLengthCtx,
    _validate_mp3_xing_length,
    _validate_mp3_xing_tail,
)

__all__ = [
    "_find_mp3_sync",
    "_id3v2_skip_size",
    "_looks_like_mp3",
    "_read_mp3_probe",
    "_validate_mp3_xing_tail",
    "read_mp3_metadata",
    "scan_mp3_frames",
]


@dataclass(frozen=True, slots=True)
class _MP3FrameCountCtx:
    sync: int
    version_id: int
    channels: int
    samples_per_frame: int
    file_size: int
    bitrate_kbps: int
    sample_rate: int
    file_name: str
    id3v2_size: int


def _read_mp3_file_bytes(path: Path) -> tuple[int, bytes]:
    try:
        file_size = path.stat().st_size
        data = path.read_bytes()
    except OSError as exc:
        raise StemArtifactError(f"cannot read MP3 metadata for {path.name}: {exc}") from exc
    return file_size, data


def _mp3_first_frame_layout(file_name: str, data: bytes) -> tuple[int, int, int, int, int, int, int, int]:
    id3v2_size = _id3v2_skip_size(data)
    sync = _find_mp3_sync(data, id3v2_size)
    if sync < 0 or sync + 4 > len(data):
        raise StemArtifactError(f"{file_name} has no MPEG audio sync word")

    header = data[sync : sync + 4]
    version_id, layer, bitrate_idx, sample_rate_idx, padding, channels = _parse_mpeg_frame_header(header)
    if layer != 3:
        raise StemArtifactError(f"{file_name} is not MPEG Layer III")

    sample_rate = _mpeg_sample_rate(version_id, sample_rate_idx)
    bitrate_kbps = _mpeg_bitrate_kbps(version_id, layer, bitrate_idx)
    if sample_rate <= 0 or channels <= 0:
        raise StemArtifactError(f"{file_name} has invalid MP3 header geometry")
    if bitrate_kbps <= 0:
        raise StemArtifactError(f"{file_name} uses free-format or invalid MP3 bitrate")

    frame_size = _mpeg_layer3_frame_size(version_id, bitrate_kbps, sample_rate, padding)
    if frame_size <= 0 or sync + frame_size > len(data):
        raise StemArtifactError(f"{file_name} has truncated MP3 frame")
    samples_per_frame = 1152 if version_id == 3 else 576
    return sync, version_id, channels, samples_per_frame, id3v2_size, sample_rate, bitrate_kbps, padding


def read_mp3_metadata(path: Path) -> WavMetadata:
    """Parse MP3 geometry from frame headers without decoding or subprocess."""
    file_size, data = _read_mp3_file_bytes(path)
    if file_size < 4:
        raise StemArtifactError(f"{path.name} is too short to be MP3")

    (
        sync,
        version_id,
        channels,
        samples_per_frame,
        id3v2_size,
        sample_rate,
        bitrate_kbps,
        _padding,
    ) = _mp3_first_frame_layout(path.name, data)

    frame_count = _mp3_frame_count(
        data,
        _MP3FrameCountCtx(
            sync=sync,
            version_id=version_id,
            channels=channels,
            samples_per_frame=samples_per_frame,
            file_size=file_size,
            bitrate_kbps=bitrate_kbps,
            sample_rate=sample_rate,
            file_name=path.name,
            id3v2_size=id3v2_size,
        ),
    )
    if frame_count <= 0:
        raise StemArtifactError(f"{path.name} has zero MP3 frames")
    return WavMetadata(
        sample_rate=sample_rate,
        frame_count=frame_count,
        channels=channels,
    )


def _mp3_frame_count(data: bytes, ctx: _MP3FrameCountCtx) -> int:
    side_info_len = _mp3_side_info_len(ctx.version_id, ctx.channels)
    xing_offset = ctx.sync + 4 + side_info_len
    if xing_offset + 8 <= len(data):
        tag = data[xing_offset : xing_offset + 4]
        if tag in {b"Xing", b"Info"}:
            flags = struct.unpack(">I", data[xing_offset + 4 : xing_offset + 8])[0]
            if flags & 0x01:
                if xing_offset + 12 > len(data):
                    raise StemArtifactError(f"{ctx.file_name} MP3 Xing/Info header is truncated")
                frames = struct.unpack(">I", data[xing_offset + 8 : xing_offset + 12])[0]
                if frames > 0:
                    _validate_mp3_xing_length(
                        _MP3XingLengthCtx(
                            file_name=ctx.file_name,
                            data=data,
                            file_size=ctx.file_size,
                            id3v2_size=ctx.id3v2_size,
                            xing_offset=xing_offset,
                            flags=flags,
                            frames=frames,
                            version_id=ctx.version_id,
                            bitrate_kbps=ctx.bitrate_kbps,
                            sample_rate=ctx.sample_rate,
                        )
                    )
                    _validate_mp3_xing_tail(
                        data,
                        file_name=ctx.file_name,
                        version_id=ctx.version_id,
                    )
                    return frames * ctx.samples_per_frame
    scanned = scan_mp3_frames(
        data,
        sync=ctx.sync,
        file_name=ctx.file_name,
        expected_version_id=ctx.version_id,
    )
    return scanned * ctx.samples_per_frame
