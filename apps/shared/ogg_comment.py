"""In-house Ogg Vorbis / Ogg Opus comment reader / writer (Apache-2.0).

Replaces the GPL ``mutagen.oggvorbis`` / ``mutagen.oggopus`` for the one thing
this project writes into Ogg files: the Vorbis comment header packet. Written
from the public specifications only (Ogg framing, RFC 3533 and
https://xiph.org/ogg/doc/framing.html; Vorbis I section 4.2 and
https://xiph.org/vorbis/doc/v-comment.html; Ogg Opus, RFC 7845 section 5.2);
no mutagen source was consulted.

Mini-PRD
--------
✔︎ R1 read the comment header of the first logical stream of an Ogg file whose
       codec is Vorbis or Opus.
  [if] the file is not Ogg, or the stream is another codec [then] OggError
  [if] a header page's CRC does not match its bytes         [then] OggError
  [if] another stream's page interleaves the headers        [then] OggError
✔︎ R2 rewrite that comment packet: the identification page is kept byte for
       byte, the comment (and, for Vorbis, setup) packets are re-paginated, and
       every later page keeps its payload and granule position; only the page
       sequence numbers (and so the CRCs) of that stream move, and only when
       the header page count changed.
  [if] a comment is set           [then] ffprobe and tinytag read it back
  [if] the comment packet grows   [then] the decoded audio is identical
  [if] the write fails midway     [then] the original file is intact
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, ClassVar

from apps.shared import vorbis_comment
from apps.shared.file_rewrite import copy_rest, rewrite_atomic
from apps.shared.vorbis_comment import VorbisCommentFields

CAPTURE = b"OggS"
_HEADER = struct.Struct("<4sBBqIII B")  # capture, version, type, granule, serial, seq, crc, nsegs
FLAG_CONTINUED = 0x01
FLAG_BOS = 0x02
FLAG_EOS = 0x04
NO_PACKET_ENDS = -1  # granule of a page on which no packet completes
_MAX_SEGMENTS = 255

VORBIS_ID = b"\x01vorbis"
VORBIS_COMMENT = b"\x03vorbis"
OPUS_ID = b"OpusHead"
OPUS_TAGS = b"OpusTags"


class OggError(vorbis_comment.VorbisCommentError):
    """An Ogg file that cannot be read or written faithfully. Never swallowed."""


@dataclass
class Page:
    header_type: int
    granule: int
    serial: int
    sequence: int
    segments: list[int]
    body: bytes

    def render(self) -> bytes:
        header = _HEADER.pack(
            CAPTURE, 0, self.header_type, self.granule, self.serial, self.sequence, 0, len(self.segments)
        )
        raw = bytearray(header + bytes(self.segments) + self.body)
        raw[22:26] = struct.pack("<I", ogg_crc(bytes(raw)))
        return bytes(raw)


@dataclass
class OggMeta(VorbisCommentFields):
    error: ClassVar[type[Exception]] = OggError
    codec: str  # "vorbis" | "opus"
    serial: int
    id_page: bytes  # the identification header page, kept byte for byte
    header_pages: int  # pages the comment + setup packets used
    setup_packets: list[bytes]  # Vorbis setup header; empty for Opus
    trailing: bytes  # bytes after the comment structure (framing bit, padding)
    audio_offset: int  # file offset of the first page after the headers
    vendor: str = ""
    comments: list[tuple[str, str]] = field(default_factory=list)


# ============================================================== CRC =========
_BIT_REVERSE = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def ogg_crc(data: bytes) -> int:
    """Ogg's CRC-32: polynomial 0x04C11DB7, not reflected, initial value 0.

    zlib computes the reflected form of the same polynomial, so the bytes are
    bit-reversed in, the result bit-reversed out, and zlib's own ``~`` pre/post
    conditioning cancelled by starting from ``0xFFFFFFFF``.
    """
    reflected = zlib.crc32(data.translate(_BIT_REVERSE), 0xFFFFFFFF) ^ 0xFFFFFFFF
    return int(f"{reflected:032b}"[::-1], 2)


# ============================================================== read ========
def _read_page(handle: BinaryIO, path: Path) -> Page | None:
    start = handle.tell()
    header = handle.read(_HEADER.size)
    if not header:
        return None
    if len(header) != _HEADER.size or header[:4] != CAPTURE:
        raise OggError(f"{path}: no Ogg page at offset {start}")
    _capture, version, header_type, granule, serial, sequence, crc, nsegs = _HEADER.unpack(header)
    if version != 0:
        raise OggError(f"{path}: unsupported Ogg version {version} at offset {start}")
    segments = list(handle.read(nsegs))
    body = handle.read(sum(segments))
    if len(segments) != nsegs or len(body) != sum(segments):
        raise OggError(f"{path}: Ogg page at offset {start} runs past the end of the file")
    page = Page(header_type, granule, serial, sequence, segments, body)
    if page.render()[22:26] != struct.pack("<I", crc):
        raise OggError(f"{path}: CRC mismatch on the Ogg page at offset {start}")
    return page


def _packets_of(page: Page) -> list[tuple[bytes, bool]]:
    """``(bytes, completed_on_this_page)`` for each packet piece on ``page``."""
    out: list[tuple[bytes, bool]] = []
    pos = 0
    current = 0
    for lace in page.segments:
        current += lace
        if lace < 255:
            out.append((page.body[pos : pos + current], True))
            pos += current
            current = 0
    if current:
        out.append((page.body[pos : pos + current], False))
    return out


def _codec_of(first: Page, path: Path) -> tuple[str, int, bytes]:
    """``(codec, header packet count, comment magic)`` of the stream ``first`` opens."""
    if not first.header_type & FLAG_BOS:
        raise OggError(f"{path}: not an Ogg stream (no beginning-of-stream page)")
    if _packets_of(first) != [(first.body, True)]:
        raise OggError(f"{path}: identification header does not sit alone on its page")
    if first.body.startswith(VORBIS_ID):
        return "vorbis", 3, VORBIS_COMMENT
    if first.body.startswith(OPUS_ID):
        return "opus", 2, OPUS_TAGS
    raise OggError(f"{path}: Ogg stream codec is neither Vorbis nor Opus")


def _header_packets(handle: BinaryIO, serial: int, count: int, path: Path) -> tuple[list[bytes], int]:
    """The ``count`` packets after the identification page, and the pages they used.

    The last one must end its page: both specs start audio on a fresh page.
    """
    packets: list[bytes] = []
    partial = b""
    pages = 0
    while len(packets) < count:
        page = _read_page(handle, path)
        if page is None:
            raise OggError(f"{path}: file ends inside the stream headers")
        if page.serial != serial:
            raise OggError(f"{path}: another logical stream interleaves the stream headers")
        if bool(page.header_type & FLAG_CONTINUED) != bool(partial):
            raise OggError(f"{path}: page continuation flag disagrees with the packet lacing")
        pages += 1
        for piece, completed in _packets_of(page):
            joined = partial + piece
            partial = b"" if completed else joined
            if completed:
                packets.append(joined)
    if partial or len(packets) != count:
        raise OggError(f"{path}: the last stream header does not end its page")
    return packets, pages


def read(path: Path) -> OggMeta:
    with path.open("rb") as handle:
        first = _read_page(handle, path)
        if first is None:
            raise OggError(f"{path}: empty file, not an Ogg stream")
        codec, header_count, comment_magic = _codec_of(first, path)
        id_page_end = handle.tell()
        packets, header_pages = _header_packets(handle, first.serial, header_count - 1, path)
        audio_offset = handle.tell()
        handle.seek(0)
        id_page = handle.read(id_page_end)

    comment_packet = packets[0]
    if not comment_packet.startswith(comment_magic):
        raise OggError(f"{path}: second {codec} packet is not the comment header")
    vendor, comments, end = vorbis_comment.decode(comment_packet, str(path), len(comment_magic))
    trailing = comment_packet[end:]
    if codec == "vorbis" and not (trailing[:1] and trailing[0] & 0x01):
        raise OggError(f"{path}: Vorbis comment header is missing its framing bit")
    return OggMeta(
        codec=codec,
        serial=first.serial,
        id_page=id_page,
        header_pages=header_pages,
        setup_packets=packets[1:],
        trailing=trailing,
        audio_offset=audio_offset,
        vendor=vendor,
        comments=comments,
    )


# ============================================================== write =======
def _paginate(packets: list[bytes], serial: int, first_sequence: int) -> list[Page]:
    """Lay ``packets`` out on pages; the last packet ends the last page."""
    laces: list[tuple[int, int, bool]] = []  # (packet index, lace value, ends packet)
    for index, packet in enumerate(packets):
        full, rest = divmod(len(packet), 255)
        laces += [(index, 255, False)] * full + [(index, rest, True)]
    pages: list[Page] = []
    pos = [0] * len(packets)
    continued = False
    for start in range(0, len(laces), _MAX_SEGMENTS):
        chunk = laces[start : start + _MAX_SEGMENTS]
        body = bytearray()
        for index, lace, _ends in chunk:
            body += packets[index][pos[index] : pos[index] + lace]
            pos[index] += lace
        ends_a_packet = any(ends for _i, _lace, ends in chunk)
        pages.append(
            Page(
                header_type=FLAG_CONTINUED if continued else 0,
                granule=0 if ends_a_packet else NO_PACKET_ENDS,
                serial=serial,
                sequence=first_sequence + len(pages),
                segments=[lace for _i, lace, _ends in chunk],
                body=bytes(body),
            )
        )
        continued = not chunk[-1][2]
    return pages


def save(path: Path, meta: OggMeta) -> None:
    """Rewrite ``path``'s comment header from ``meta``; audio pages keep their payload."""
    magic = VORBIS_COMMENT if meta.codec == "vorbis" else OPUS_TAGS
    comment_packet = magic + vorbis_comment.encode(meta.vendor, meta.comments) + meta.trailing
    pages = _paginate([comment_packet, *meta.setup_packets], meta.serial, first_sequence=1)
    shift = len(pages) - meta.header_pages

    def build(src: BinaryIO, out: BinaryIO) -> None:
        out.write(meta.id_page)
        for header_page in pages:
            out.write(header_page.render())
        if shift == 0:
            copy_rest(src, out, meta.audio_offset)
            return
        src.seek(meta.audio_offset)
        renumbering = True
        while (page := _read_page(src, path)) is not None:
            if renumbering and page.serial == meta.serial:
                page.sequence += shift
                renumbering = not page.header_type & FLAG_EOS
            out.write(page.render())

    rewrite_atomic(path, build)


__all__ = ["OggError", "OggMeta", "Page", "ogg_crc", "read", "save"]
