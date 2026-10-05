"""Vorbis comment encode / decode, shared by the FLAC and Ogg tag writers (Apache-2.0).

Written from the public specification (https://xiph.org/vorbis/doc/v-comment.html);
FLAC carries this structure in its VORBIS_COMMENT block, Ogg Vorbis and Ogg Opus
in their second header packet.
"""
from __future__ import annotations

import struct
from typing import ClassVar


class VorbisCommentError(Exception):
    """A Vorbis comment structure that cannot be read or written faithfully."""


class VorbisCommentFields:
    """``get`` / ``first`` / ``set`` over a ``comments`` list of ``(name, value)``.

    Names compare case-insensitively (the spec says so); ``set`` replaces every
    existing entry of that name and keeps the spelling it was given.
    """

    comments: list[tuple[str, str]]
    #: Raised by :meth:`set` for an invalid name; a container module narrows it.
    error: ClassVar[type[Exception]] = VorbisCommentError

    def get(self, key: str) -> list[str]:
        wanted = key.upper()
        return [value for name, value in self.comments if name.upper() == wanted]

    def first(self, key: str) -> str | None:
        values = [v.strip() for v in self.get(key) if v.strip()]
        return values[0] if values else None

    def set(self, key: str, value: str) -> None:
        if not key or "=" in key or any(not 0x20 <= ord(c) <= 0x7D for c in key):
            raise self.error(f"invalid Vorbis comment field name {key!r}")
        wanted = key.upper()
        self.comments = [(n, v) for n, v in self.comments if n.upper() != wanted]
        self.comments.append((key, value))


def _take(data: bytes, offset: int, size: int, where: str) -> bytes:
    if offset + size > len(data):
        raise VorbisCommentError(f"{where}: truncated Vorbis comment at byte {offset}")
    return data[offset : offset + size]


def _u32(data: bytes, offset: int, where: str) -> int:
    return int.from_bytes(_take(data, offset, 4, where), "little")


def decode(data: bytes, where: str, offset: int = 0) -> tuple[str, list[tuple[str, str]], int]:
    """``(vendor, comments, end_offset)`` of the structure starting at ``offset``."""
    vendor_len = _u32(data, offset, where)
    vendor = _take(data, offset + 4, vendor_len, where).decode("utf-8", errors="replace")
    offset += 4 + vendor_len
    count = _u32(data, offset, where)
    offset += 4
    comments = []
    for _ in range(count):
        length = _u32(data, offset, where)
        entry = _take(data, offset + 4, length, where).decode("utf-8", errors="replace")
        offset += 4 + length
        name, sep, value = entry.partition("=")
        if sep:
            comments.append((name, value))
    return vendor, comments, offset


def encode(vendor: str, comments: list[tuple[str, str]]) -> bytes:
    vendor_raw = vendor.encode("utf-8")
    out = [struct.pack("<I", len(vendor_raw)), vendor_raw, struct.pack("<I", len(comments))]
    for name, value in comments:
        entry = f"{name}={value}".encode()
        out += [struct.pack("<I", len(entry)), entry]
    return b"".join(out)


__all__ = ["VorbisCommentError", "VorbisCommentFields", "decode", "encode"]
