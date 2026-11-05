"""Bounded RIFF/FORM chunk-header reader for the playable-audio probes."""

from __future__ import annotations

from typing import BinaryIO, Literal

Endian = Literal["little", "big"]


class UnplayableAudioError(Exception):
    """Raised when a path is not playable audio for library ingest."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def read_chunk(
    handle: BinaryIO,
    offset: int,
    file_size: int,
    container_end: int,
    endian: Endian,
) -> tuple[bytes, int, int, int]:
    """Read one 8-byte chunk header and return its bounded layout.

    Returns ``(chunk_id, payload_start, payload_size, next_offset)``.
    ``next_offset`` includes the odd-size pad byte when that pad still lies
    before ``container_end``. Zero-size chunks still advance past the header.
    """
    bound = min(file_size, container_end)
    if offset + 8 > bound:
        raise UnplayableAudioError("truncated chunk header")
    handle.seek(offset)
    header = handle.read(8)
    if len(header) < 8:
        raise UnplayableAudioError("truncated chunk header")
    chunk_id = header[:4]
    payload_size = int.from_bytes(header[4:8], endian)
    payload_start = offset + 8
    payload_end = payload_start + payload_size
    if payload_end > bound:
        raise UnplayableAudioError("truncated chunk payload")
    next_offset = payload_end
    if payload_size % 2 and next_offset < container_end:
        next_offset += 1
        if next_offset > bound:
            raise UnplayableAudioError("truncated chunk pad")
    return chunk_id, payload_start, payload_size, next_offset
