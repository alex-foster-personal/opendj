"""MPEG Layer III low-level helpers (sync, header parse, frame size)."""

from __future__ import annotations

import struct
from pathlib import Path

from apps.stems.artifact_types import StemArtifactError

_MPEG1_L3_BITRATES_KBPS = (
    0,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    160,
    192,
    224,
    256,
    320,
)
_MPEG2_L3_BITRATES_KBPS = (
    0,
    8,
    16,
    24,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    144,
    160,
)
_MPEG_SAMPLE_RATES = {
    3: (44_100, 48_000, 32_000),
    2: (22_050, 24_000, 16_000),
    0: (11_025, 12_000, 8_000),
}


def _read_mp3_probe(path: Path, initial: bytes) -> bytes:
    try:
        file_size = path.stat().st_size
        probe_len = min(file_size, 256 * 1024)
        if len(initial) >= probe_len:
            return initial[:probe_len]
        with path.open("rb") as source:
            return source.read(probe_len)
    except OSError:
        return initial


def _id3v2_skip_size(data: bytes) -> int:
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
    return 10 + size


def _find_mp3_sync(data: bytes, start: int) -> int:
    limit = len(data) - 1
    for index in range(start, limit):
        if data[index] == 0xFF and (data[index + 1] & 0xE0) == 0xE0:
            return index
    return -1


def _looks_like_mp3(data: bytes) -> bool:
    return _find_mp3_sync(data, _id3v2_skip_size(data)) >= 0


def _parse_mpeg_frame_header(header: bytes) -> tuple[int, int, int, int, int, int]:
    if len(header) != 4 or header[0] != 0xFF or (header[1] & 0xE0) != 0xE0:
        raise StemArtifactError("invalid MPEG frame header")
    version_id = (header[1] >> 3) & 0x03
    layer = 4 - ((header[1] >> 1) & 0x03)
    bitrate_idx = (header[2] >> 4) & 0x0F
    sample_rate_idx = (header[2] >> 2) & 0x03
    padding = (header[2] >> 1) & 0x01
    channel_mode = (header[3] >> 6) & 0x03
    mode_extension = (header[3] >> 4) & 0x03
    channels = _mpeg_channels(channel_mode, mode_extension)
    return version_id, layer, bitrate_idx, sample_rate_idx, padding, channels


def _mpeg_channels(channel_mode: int, mode_extension: int) -> int:
    del mode_extension
    if channel_mode == 3:
        return 1
    return 2


def _mpeg_sample_rate(version_id: int, sample_rate_idx: int) -> int:
    if sample_rate_idx == 3:
        return 0
    try:
        return _MPEG_SAMPLE_RATES[version_id][sample_rate_idx]
    except (KeyError, IndexError):
        return 0


def _mpeg_bitrate_kbps(version_id: int, layer: int, bitrate_idx: int) -> int:
    if bitrate_idx in {0, 15}:
        return 0
    if layer != 3:
        return 0
    table = _MPEG1_L3_BITRATES_KBPS if version_id == 3 else _MPEG2_L3_BITRATES_KBPS
    return table[bitrate_idx]


def _mpeg_layer3_frame_size(version_id: int, bitrate_kbps: int, sample_rate: int, padding: int) -> int:
    if sample_rate <= 0 or bitrate_kbps <= 0:
        return 0
    if version_id == 3:
        return (144_000 * bitrate_kbps) // sample_rate + padding
    return (72_000 * bitrate_kbps) // sample_rate + padding


def _mp3_side_info_len(version_id: int, channels: int) -> int:
    if version_id == 3:
        return 17 if channels == 1 else 32
    return 9 if channels == 1 else 17


def _mp3_audio_end(data: bytes) -> int:
    """Byte offset where MPEG audio ends, excluding ID3v1 and APEv2 trailers."""
    end = len(data)
    if end >= 128 and data[end - 128 : end - 125] == b"TAG":
        end -= 128
    if end >= 32 and data[end - 32 : end - 24] == b"APETAGEX":
        tag_body_size = struct.unpack("<I", data[end - 20 : end - 16])[0]
        end -= 32 + tag_body_size
    return end


def _mp3_header_geometry_at(
    data: bytes,
    offset: int,
) -> tuple[int, int, int, int, int, int] | None:
    if offset + 4 > len(data):
        return None
    if data[offset] != 0xFF or (data[offset + 1] & 0xE0) != 0xE0:
        return None
    try:
        return _parse_mpeg_frame_header(data[offset : offset + 4])
    except StemArtifactError:
        return None


def _mp3_layer3_frame_size_at(
    data: bytes,
    offset: int,
    *,
    expected_version_id: int | None,
    expected_layer: int | None,
) -> int | None:
    parsed = _mp3_header_geometry_at(data, offset)
    if parsed is None:
        return None
    version_id, layer, bitrate_idx, sample_rate_idx, padding, _channels = parsed
    if expected_layer is not None and layer != expected_layer:
        return None
    if expected_version_id is not None and version_id != expected_version_id:
        return None
    sample_rate = _mpeg_sample_rate(version_id, sample_rate_idx)
    bitrate_kbps = _mpeg_bitrate_kbps(version_id, layer, bitrate_idx)
    if sample_rate <= 0 or bitrate_kbps <= 0:
        return None
    frame_size = _mpeg_layer3_frame_size(version_id, bitrate_kbps, sample_rate, padding)
    if frame_size <= 0:
        return None
    return frame_size


def _mp3_chain_reaches(
    data: bytes,
    start: int,
    audio_end: int,
    *,
    version_id: int,
    expected_layer: int,
) -> bool:
    offset = start
    while offset < audio_end:
        frame_size = _mp3_layer3_frame_size_at(
            data,
            offset,
            expected_version_id=version_id,
            expected_layer=expected_layer,
        )
        if frame_size is None or offset + frame_size > len(data):
            break
        offset += frame_size
    return offset == audio_end


def _mp3_trailing_data_allowed(data: bytes, offset: int) -> bool:
    remaining = len(data) - offset
    return (
        remaining == 0
        or (remaining == 128 and data[offset : offset + 3] == b"TAG")
        or (remaining >= 32 and data[-32:-24] == b"APETAGEX")
    )
