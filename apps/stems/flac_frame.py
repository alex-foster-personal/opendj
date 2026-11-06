"""FLAC frame geometry helpers (CRC, UTF-8 block numbers, sync headers)."""

from __future__ import annotations

import struct

_FLAC_FIXED_BLOCK_SIZES: dict[int, int] = {
    1: 192,
    2: 576,
    3: 1152,
    4: 2304,
    5: 4608,
}


def _flac_crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _flac_crc16(data: bytes) -> int:
    """CRC-16/IBM for FLAC frame footers (poly 0x8005, init 0, MSB-first)."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _flac_utf8_total_and_value(first: int) -> tuple[int, int] | None:
    if first & 0x80 == 0:
        return 1, first
    if (first & 0xE0) == 0xC0:
        return 2, first & 0x1F
    if (first & 0xF0) == 0xE0:
        return 3, first & 0x0F
    if (first & 0xF8) == 0xF0:
        return 4, first & 0x07
    if (first & 0xFC) == 0xF8:
        return 5, first & 0x03
    if (first & 0xFE) == 0xFC:
        return 6, first & 0x01
    if first == 0xFE:
        return 7, 0
    return None


def _flac_read_utf8_number(data: bytes, offset: int) -> tuple[int, int] | None:
    if offset >= len(data):
        return None
    first = data[offset]
    meta = _flac_utf8_total_and_value(first)
    if meta is None:
        return None
    total_bytes, value = meta
    if offset + total_bytes > len(data):
        return None
    for index in range(1, total_bytes):
        follow = data[offset + index]
        if (follow & 0xC0) != 0x80:
            return None
        value = (value << 6) | (follow & 0x3F)
    return value, offset + total_bytes


def _flac_block_size_from_header(
    blocksize_code: int,
    data: bytes,
    offset: int,
    *,
    min_blocksize: int,
    max_blocksize: int,
) -> tuple[int, int] | None:
    del min_blocksize, max_blocksize
    if blocksize_code == 0:
        return None
    if blocksize_code in _FLAC_FIXED_BLOCK_SIZES:
        return _FLAC_FIXED_BLOCK_SIZES[blocksize_code], offset
    if blocksize_code == 6:
        if offset >= len(data):
            return None
        return data[offset] + 1, offset + 1
    if blocksize_code == 7:
        if offset + 2 > len(data):
            return None
        return struct.unpack(">H", data[offset : offset + 2])[0] + 1, offset + 2
    if 8 <= blocksize_code <= 15:
        return 256 << (blocksize_code - 8), offset
    return None


def _flac_frame_sync_prefix_ok(data: bytes, sync_offset: int) -> tuple[bool, int, int, int] | None:
    """Return (variable_blocksize, blocksize_code, header_start, offset_after_sync) or None."""
    if sync_offset + 4 > len(data):
        return None
    if data[sync_offset] != 0xFF or data[sync_offset + 1] not in {0xF8, 0xF9}:
        return None
    variable_blocksize = (data[sync_offset + 1] & 0x01) != 0
    blocksize_code = (data[sync_offset + 2] >> 4) & 0x0F
    if data[sync_offset + 3] & 0x01:
        return None
    return variable_blocksize, blocksize_code, sync_offset, sync_offset + 4


def _flac_end_sample_from_parts(
    *,
    number: int,
    block_size: int,
    variable_blocksize: bool,
    max_blocksize: int,
) -> int:
    return number + block_size if variable_blocksize else number * max_blocksize + block_size


def _flac_valid_frame_header_at(
    data: bytes,
    sync_offset: int,
    *,
    min_blocksize: int,
    max_blocksize: int,
) -> tuple[int, int, bool] | None:
    """Return (end_sample, header_end_offset, variable_blocksize) if valid."""
    prefix = _flac_frame_sync_prefix_ok(data, sync_offset)
    if prefix is None:
        return None
    variable_blocksize, blocksize_code, header_start, offset = prefix
    parsed = _flac_read_utf8_number(data, offset)
    if parsed is None:
        return None
    number, offset = parsed
    block_parsed = _flac_block_size_from_header(
        blocksize_code,
        data,
        offset,
        min_blocksize=min_blocksize,
        max_blocksize=max_blocksize,
    )
    if block_parsed is None:
        return None
    block_size, offset = block_parsed
    if block_size <= 0 or offset >= len(data):
        return None
    header_bytes = data[header_start:offset]
    if _flac_crc8(header_bytes) != data[offset]:
        return None
    end_sample = _flac_end_sample_from_parts(
        number=number,
        block_size=block_size,
        variable_blocksize=variable_blocksize,
        max_blocksize=max_blocksize,
    )
    return end_sample, offset + 1, variable_blocksize


def _flac_stream_end(data: bytes) -> int:
    """End of FLAC audio excluding a trailing ID3v1 tag, if present."""
    end = len(data)
    if end >= 128 and data[end - 128 : end - 125] == b"TAG":
        end -= 128
    return end


def _flac_frame_crc16_valid(data: bytes, frame_start: int, stream_end: int) -> bool:
    """True when the frame ending at stream_end has a valid CRC-16 footer."""
    if frame_start >= stream_end or stream_end - frame_start < 2:
        return False
    footer = stream_end - 2
    expected = struct.unpack(">H", data[footer:stream_end])[0]
    return _flac_crc16(data[frame_start:footer]) == expected
