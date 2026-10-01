"""In-house FLAC metadata (Vorbis comment + PICTURE) reader / writer (Apache-2.0).

Replaces the GPL ``mutagen.flac`` for the one thing this project writes into
FLAC files: Vorbis comments. Written from the public format specifications
only (https://xiph.org/flac/format.html, RFC 9639 section 8;
https://xiph.org/vorbis/doc/v-comment.html); no mutagen source was consulted.

Mini-PRD
--------
✔︎ R1 read the metadata blocks of a FLAC file (an ID3v2 tag in front, which
       some taggers prepend, is skipped and preserved).
  [if] the stream is not FLAC            [then] FlacError, never a guess
  [if] a block length runs past the file [then] FlacError
✔︎ R2 rewrite the VORBIS_COMMENT block (and optionally add PICTURE blocks)
       keeping STREAMINFO, SEEKTABLE, CUESHEET, APPLICATION and existing
       PICTURE blocks byte-for-byte, and the audio frames untouched.
  [if] the new metadata fits the old [then] the audio offset does not move
  [if] the write fails midway        [then] the original file is intact
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from apps.shared.file_rewrite import replace_head

MAGIC = b"fLaC"
BLOCK_STREAMINFO = 0
BLOCK_PADDING = 1
BLOCK_VORBIS_COMMENT = 4
BLOCK_PICTURE = 6
DEFAULT_PADDING = 4096
_MAX_BLOCK = (1 << 24) - 1


class FlacError(Exception):
    """A FLAC file that cannot be read or written faithfully. Never swallowed."""


@dataclass
class Block:
    block_type: int
    data: bytes


@dataclass
class FlacMeta:
    prefix: bytes  # bytes before ``fLaC`` (an ID3v2 tag some taggers prepend)
    blocks: list[Block] = field(default_factory=list)
    audio_offset: int = 0
    vendor: str = "music-dj-tools"
    comments: list[tuple[str, str]] = field(default_factory=list)

    def get(self, key: str) -> list[str]:
        wanted = key.upper()
        return [value for name, value in self.comments if name.upper() == wanted]

    def first(self, key: str) -> str | None:
        values = [v.strip() for v in self.get(key) if v.strip()]
        return values[0] if values else None

    def set(self, key: str, value: str) -> None:
        if not key or "=" in key or any(not 0x20 <= ord(c) <= 0x7D for c in key):
            raise FlacError(f"invalid Vorbis comment field name {key!r}")
        wanted = key.upper()
        self.comments = [(n, v) for n, v in self.comments if n.upper() != wanted]
        self.comments.append((key, value))

    def add_picture(self, mime: str, picture_type: int, data: bytes, description: str = "") -> None:
        self.blocks.append(Block(BLOCK_PICTURE, encode_picture(mime, picture_type, data, description)))


# ============================================================== read ========
def read(path: Path) -> FlacMeta:
    with path.open("rb") as handle:
        raw_head = handle.read(10)
        prefix_len = 0
        if raw_head[:3] == b"ID3" and len(raw_head) == 10:
            size = 0
            for byte in raw_head[6:10]:
                size = (size << 7) | (byte & 0x7F)
            prefix_len = 10 + size + (10 if raw_head[5] & 0x10 else 0)
        handle.seek(0)
        prefix = handle.read(prefix_len)
        if handle.read(4) != MAGIC:
            raise FlacError(f"{path}: not a FLAC stream (missing fLaC marker)")
        meta = FlacMeta(prefix=prefix)
        last = False
        while not last:
            header = handle.read(4)
            if len(header) != 4:
                raise FlacError(f"{path}: metadata ends before the last-block flag")
            last = bool(header[0] & 0x80)
            block_type = header[0] & 0x7F
            length = int.from_bytes(header[1:4], "big")
            data = handle.read(length)
            if len(data) != length:
                raise FlacError(f"{path}: metadata block {block_type} runs past the end of the file")
            if block_type == BLOCK_VORBIS_COMMENT:
                meta.vendor, meta.comments = _decode_vorbis_comment(data, path)
            elif block_type != BLOCK_PADDING:
                meta.blocks.append(Block(block_type, data))
        meta.audio_offset = handle.tell()
    if not meta.blocks or meta.blocks[0].block_type != BLOCK_STREAMINFO:
        raise FlacError(f"{path}: first metadata block is not STREAMINFO")
    return meta


def _decode_vorbis_comment(data: bytes, path: Path) -> tuple[str, list[tuple[str, str]]]:
    try:
        offset = 0
        (vendor_len,) = struct.unpack_from("<I", data, offset)
        offset += 4
        vendor = data[offset : offset + vendor_len].decode("utf-8", errors="replace")
        offset += vendor_len
        (count,) = struct.unpack_from("<I", data, offset)
        offset += 4
        comments = []
        for _ in range(count):
            (length,) = struct.unpack_from("<I", data, offset)
            offset += 4
            entry = data[offset : offset + length].decode("utf-8", errors="replace")
            offset += length
            name, sep, value = entry.partition("=")
            if sep:
                comments.append((name, value))
        return vendor, comments
    except struct.error as exc:
        raise FlacError(f"{path}: truncated VORBIS_COMMENT block") from exc


# ============================================================== write =======
def encode_vorbis_comment(vendor: str, comments: list[tuple[str, str]]) -> bytes:
    vendor_raw = vendor.encode("utf-8")
    out = [struct.pack("<I", len(vendor_raw)), vendor_raw, struct.pack("<I", len(comments))]
    for name, value in comments:
        entry = f"{name}={value}".encode()
        out += [struct.pack("<I", len(entry)), entry]
    return b"".join(out)


def encode_picture(mime: str, picture_type: int, data: bytes, description: str = "") -> bytes:
    mime_raw = mime.encode("ascii")
    desc_raw = description.encode("utf-8")
    return b"".join(
        [
            struct.pack(">I", picture_type),
            struct.pack(">I", len(mime_raw)), mime_raw,
            struct.pack(">I", len(desc_raw)), desc_raw,
            struct.pack(">IIII", 0, 0, 0, 0),  # width, height, depth, colours: unknown
            struct.pack(">I", len(data)), data,
        ]
    )


def save(path: Path, meta: FlacMeta) -> None:
    """Rewrite ``path``'s metadata from ``meta``; audio frames are copied as-is."""
    blocks = [meta.blocks[0], Block(BLOCK_VORBIS_COMMENT, encode_vorbis_comment(meta.vendor, meta.comments))]
    blocks += meta.blocks[1:]
    body = b"".join(_render_block(b, last=False) for b in blocks)
    old_meta_size = meta.audio_offset - len(meta.prefix) - len(MAGIC)
    room = old_meta_size - len(body) - 4
    padding = room if room >= 0 else DEFAULT_PADDING
    head = meta.prefix + MAGIC + body + _render_block(Block(BLOCK_PADDING, b"\x00" * padding), last=True)
    replace_head(path, head, meta.audio_offset)


def _render_block(block: Block, *, last: bool) -> bytes:
    if len(block.data) > _MAX_BLOCK:
        raise FlacError(f"metadata block {block.block_type} is {len(block.data)} bytes, over the 16 MiB limit")
    return bytes([(0x80 if last else 0) | block.block_type]) + len(block.data).to_bytes(3, "big") + block.data



__all__ = [
    "Block",
    "FlacError",
    "FlacMeta",
    "encode_picture",
    "encode_vorbis_comment",
    "read",
    "save",
]
