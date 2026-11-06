"""Sequential MP3 Layer III frame scan (frame counting)."""

from __future__ import annotations

from apps.stems.artifact_types import StemArtifactError
from apps.stems.mp3_frame import (
    _mp3_trailing_data_allowed,
    _mpeg_bitrate_kbps,
    _mpeg_layer3_frame_size,
    _mpeg_sample_rate,
    _parse_mpeg_frame_header,
)


def _mp3_parse_layer3_frame_at(
    data: bytes,
    offset: int,
    *,
    file_name: str,
    expected_version_id: int,
    expected_layer: int,
    expected_sample_rate: int | None,
) -> tuple[int, int]:
    """Return (next_offset, sample_rate). Raises StemArtifactError on invalid frame."""
    try:
        version_id, layer, bitrate_idx, sample_rate_idx, padding, _channels = _parse_mpeg_frame_header(
            data[offset : offset + 4]
        )
    except StemArtifactError as exc:
        if _mp3_trailing_data_allowed(data, offset):
            return offset, -1
        raise StemArtifactError(f"{file_name} invalid MP3 frame header at byte offset {offset}") from exc
    if layer != expected_layer:
        raise StemArtifactError(f"{file_name} MP3 layer changed at byte offset {offset}")
    if version_id != expected_version_id:
        raise StemArtifactError(f"{file_name} MP3 version changed at byte offset {offset}")
    sample_rate = _mpeg_sample_rate(version_id, sample_rate_idx)
    bitrate_kbps = _mpeg_bitrate_kbps(version_id, layer, bitrate_idx)
    if sample_rate <= 0 or bitrate_kbps <= 0:
        raise StemArtifactError(f"{file_name} invalid MP3 frame geometry at byte offset {offset}")
    if expected_sample_rate is not None and sample_rate != expected_sample_rate:
        raise StemArtifactError(f"{file_name} MP3 sample rate changed at byte offset {offset}")
    frame_size = _mpeg_layer3_frame_size(version_id, bitrate_kbps, sample_rate, padding)
    if frame_size <= 0 or offset + frame_size > len(data):
        raise StemArtifactError(f"{file_name} truncated MP3 frame at byte offset {offset}")
    return offset + frame_size, sample_rate


def _mp3_consume_frame(
    data: bytes,
    offset: int,
    *,
    file_name: str,
    expected_version_id: int,
    expected_layer: int,
    expected_sample_rate: int | None,
) -> tuple[int, int]:
    """One valid Layer III frame: return (next_offset, sample_rate_hz); sample_rate -1 means stop."""
    if data[offset] != 0xFF or (data[offset + 1] & 0xE0) != 0xE0:
        if _mp3_trailing_data_allowed(data, offset):
            return offset, -1
        raise StemArtifactError(f"{file_name} invalid MP3 frame sync at byte offset {offset}")
    return _mp3_parse_layer3_frame_at(
        data,
        offset,
        file_name=file_name,
        expected_version_id=expected_version_id,
        expected_layer=expected_layer,
        expected_sample_rate=expected_sample_rate,
    )


def scan_mp3_frames(
    data: bytes,
    *,
    sync: int,
    file_name: str,
    expected_version_id: int,
    expected_layer: int = 3,
) -> int:
    """Walk MPEG Layer III frame headers from sync to EOF and count valid frames."""
    offset = sync
    frame_count = 0
    expected_sample_rate: int | None = None
    while offset + 4 <= len(data):
        next_offset, sr = _mp3_consume_frame(
            data,
            offset,
            file_name=file_name,
            expected_version_id=expected_version_id,
            expected_layer=expected_layer,
            expected_sample_rate=expected_sample_rate,
        )
        if sr < 0:
            break
        if expected_sample_rate is None:
            expected_sample_rate = sr
        offset = next_offset
        frame_count += 1
    if frame_count == 0:
        raise StemArtifactError(f"{file_name} has zero MP3 frames")
    if offset < len(data) and not _mp3_trailing_data_allowed(data, offset):
        raise StemArtifactError(f"{file_name} unexpected trailing data at byte offset {offset}")
    return frame_count
