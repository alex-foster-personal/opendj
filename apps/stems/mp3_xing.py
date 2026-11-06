"""MP3 Xing/Info header validation (tail chain and declared length)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from apps.stems.artifact_types import StemArtifactError
from apps.stems.mp3_frame import (
    _mp3_audio_end,
    _mp3_chain_reaches,
    _mpeg_bitrate_kbps,
    _mpeg_layer3_frame_size,
    _mpeg_sample_rate,
    _parse_mpeg_frame_header,
)

_MP3_XING_TAIL_LOOKBACK = 4 * 2000
"""Fixed byte window for Xing-path tail validation (~4 max-sized Layer III frames)."""


@dataclass(frozen=True, slots=True)
class _MP3XingLengthCtx:
    file_name: str
    data: bytes
    file_size: int
    id3v2_size: int
    xing_offset: int
    flags: int
    frames: int
    version_id: int
    bitrate_kbps: int
    sample_rate: int


def _mp3_xing_candidate_ok(
    data: bytes,
    index: int,
    *,
    version_id: int,
    expected_layer: int,
    audio_end: int,
) -> bool:
    if index + 1 >= len(data):
        return False
    if data[index] != 0xFF or (data[index + 1] & 0xE0) != 0xE0:
        return False
    try:
        header_version, layer, bitrate_idx, sample_rate_idx, _padding, _channels = _parse_mpeg_frame_header(
            data[index : index + 4]
        )
    except StemArtifactError:
        return False
    if layer != expected_layer or header_version != version_id:
        return False
    sample_rate = _mpeg_sample_rate(header_version, sample_rate_idx)
    bitrate_kbps = _mpeg_bitrate_kbps(header_version, layer, bitrate_idx)
    if sample_rate <= 0 or bitrate_kbps <= 0:
        return False
    return _mp3_chain_reaches(
        data,
        index,
        audio_end,
        version_id=version_id,
        expected_layer=expected_layer,
    )


def _validate_mp3_xing_tail(
    data: bytes,
    *,
    file_name: str,
    version_id: int,
    expected_layer: int = 3,
) -> None:
    """Reject Xing-trusted MP3s whose tail is not real frame-aligned audio."""
    audio_end = _mp3_audio_end(data)
    lookback = _MP3_XING_TAIL_LOOKBACK
    window_start = max(0, audio_end - lookback)
    for index in range(audio_end - 1, window_start - 1, -1):
        if _mp3_xing_candidate_ok(
            data,
            index,
            version_id=version_id,
            expected_layer=expected_layer,
            audio_end=audio_end,
        ):
            return
    raise StemArtifactError(
        f"{file_name} MP3 Xing/Info tail check failed to find a valid frame chain "
        f"reaching audio end at byte {audio_end} within the last {lookback} bytes"
    )


def _validate_mp3_xing_length(ctx: _MP3XingLengthCtx) -> None:
    audio_bytes = ctx.file_size - ctx.id3v2_size
    field_offset = ctx.xing_offset + 8
    if ctx.flags & 0x01:
        field_offset += 4
    if ctx.flags & 0x02:
        if field_offset + 4 > len(ctx.data):
            raise StemArtifactError(f"{ctx.file_name} MP3 Xing/Info header declares bytes but is truncated")
        xing_bytes = struct.unpack(">I", ctx.data[field_offset : field_offset + 4])[0]
        if audio_bytes < xing_bytes:
            raise StemArtifactError(
                f"{ctx.file_name} MP3 declared audio length {xing_bytes} bytes "
                f"(id3v2-adjusted) but on-disk audio is {audio_bytes} bytes"
            )
        return
    min_frame_bytes = _mpeg_layer3_frame_size(
        ctx.version_id, ctx.bitrate_kbps, ctx.sample_rate, padding=0
    )
    expected_min = int(ctx.frames * min_frame_bytes * 0.9)
    if audio_bytes < expected_min:
        raise StemArtifactError(
            f"{ctx.file_name} MP3 on-disk audio is {audio_bytes} bytes but "
            f"Xing/Info frames field implies at least {expected_min} bytes"
        )
