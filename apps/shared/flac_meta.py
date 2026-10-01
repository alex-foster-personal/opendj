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
from typing import ClassVar

from apps.shared import vorbis_comment
from apps.shared.file_rewrite import replace_head
from apps.shared.vorbis_comment import VorbisCommentFields

MAGIC = b"fLaC"
BLOCK_STREAMINFO = 0
BLOCK_PADDING = 1
BLOCK_VORBIS_COMMENT = 4
BLOCK_PICTURE = 6
DEFAULT_PADDING = 4096
_MAX_BLOCK = (1 << 24) - 1


class FlacError(vorbis_comment.VorbisCommentError):
    """A FLAC file that cannot be read or written faithfully. Never swallowed."""


@dataclass
class Block:
    block_type: int
    data: bytes


@dataclass
class FlacMeta(VorbisCommentFields):
    error: ClassVar[type[Exception]] = FlacError
    prefix: bytes  # bytes before ``fLaC`` (an ID3v2 tag some taggers prepend)
    blocks: list[Block] = field(default_factory=list)
    audio_offset: int = 0
    vendor: str = "music-dj-tools"
    comments: list[tuple[str, str]] = field(default_factory=list)

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
        vendor, comments, _end = vorbis_comment.decode(data, str(path))
    except vorbis_comment.VorbisCommentError as exc:
        raise FlacError(f"{path}: truncated VORBIS_COMMENT block") from exc
    return vendor, comments


# ============================================================== write =======
encode_vorbis_comment = vorbis_comment.encode


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
