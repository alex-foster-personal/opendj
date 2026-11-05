"""In-house ID3v2.3 / ID3v2.4 reader + writer for MP3 files (Apache-2.0).

Replaces the GPL ``mutagen.id3`` for the frames this project writes: text
frames (TIT2, TPE1, TALB, TCON, TBPM, TKEY, TSRC), TXXX, COMM, POPM, APIC and
GEOB (Serato ``Markers2`` / ``BeatGrid`` / ``Overview``). Written from the
public ID3v2 specification only (https://id3.org/id3v2.3.0,
https://id3.org/id3v2.4.0-structure, https://id3.org/id3v2.4.0-frames); no
mutagen source was consulted.

Mini-PRD
--------
✔︎ R1 read every frame of a v2.3 / v2.4 tag at the start of an MP3, undoing
       tag-level (v2.3) and frame-level (v2.4) unsynchronisation and skipping
       the extended header.
  [if] the file has no ID3v2 tag       [then] :func:`read_tag` returns ``None``
  [if] the tag is v2.2 (3-char frames) [then] Id3Error, never a guess
  [if] a frame size runs past the tag  [then] Id3Error, never a truncated frame
✔︎ R2 write a tag back preserving every frame this module did not touch,
       byte-for-byte, in the tag's own version; audio bytes are untouched.
  [if] a new tag is written            [then] the audio after it is identical
  [if] the new frames fit the old tag  [then] the tag keeps its size (padding)
  [if] the write fails midway          [then] the original file is intact
       (temp file + ``os.replace``)
✔︎ R3 a frame this module cannot decode (compressed / encrypted) is kept
       opaque and re-emitted unchanged; it is never dropped.
"""

from __future__ import annotations

import functools
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from apps.shared.file_rewrite import replace_head

HEADER_SIZE = 10
FOOTER_SIZE = 10
DEFAULT_PADDING = 2048
NEW_TAG_VERSION_DEFAULT = 3
"""v2.3 is what Serato DJ Pro writes and what every DJ app reads."""

# Frame format-flag bits (second flag byte).
_V23_COMPRESSED = 0x80
_V23_ENCRYPTED = 0x40
_V23_GROUPED = 0x20
_V24_GROUPED = 0x40
_V24_COMPRESSED = 0x08
_V24_ENCRYPTED = 0x04
_V24_UNSYNC = 0x02
_V24_DATA_LENGTH = 0x01
# Format-flag bits each version leaves undefined: v2.3 defines 0x80/0x40/0x20, v2.4 defines 0x40/0x08/0x04/0x02/0x01.
_RESERVED_FORMAT_FLAGS = {3: 0x1F, 4: 0xB0}
# Status-flag bits each version leaves undefined: v2.3 defines 0xE0, v2.4 defines 0x70.
_RESERVED_STATUS_FLAGS = {3: 0x1F, 4: 0x8F}

_ENC_LATIN1 = 0
_ENC_UTF16 = 1
_ENC_UTF16BE = 2
_ENC_UTF8 = 3


class Id3Error(Exception):
    """A tag that cannot be read or written faithfully. Never swallowed."""


@dataclass
class Frame:
    """One ID3v2 frame. ``data`` is the decoded body (no unsync, no DLI)."""

    frame_id: str
    data: bytes
    status_flags: int = 0
    format_flags: int = 0
    #: The tag's major version the flags were read under; 0 for a frame
    #: built in memory, whose flags are then read under both versions.
    major: int = 0
    #: The grouping identity byte, held apart from ``data`` and written back
    #: in front of it; None when the frame is not grouped.
    group_id: int | None = None

    @property
    def opaque(self) -> bool:
        """Compressed or encrypted: kept verbatim, never decoded.

        The bits differ by version, and v2.4's grouping bit (0x40) is v2.3's
        encryption bit, so the flags are read under the frame's own version.
        """
        return bool(self.format_flags & _sealed_mask(self.major))


@dataclass
class Id3Tag:
    version: int  # major version: 3 or 4
    frames: list[Frame] = field(default_factory=list)
    #: Total bytes the tag occupied on disk (header + body + footer), 0 if new.
    original_size: int = 0

    # ----- typed accessors -------------------------------------------------
    def text(self, frame_id: str) -> list[str]:
        for frame in self.frames:
            if frame.frame_id == frame_id and not frame.opaque:
                return _decode_text_values(frame.data, frame.major)
        return []

    def first_text(self, frame_id: str) -> str | None:
        values = [v for v in self.text(frame_id) if v.strip()]
        return values[0].strip() if values else None

    def txxx(self, description: str) -> str | None:
        wanted = description.upper()
        for frame in self._decodable("TXXX"):
            desc, values = _decode_txxx(frame.data, frame.major)
            if desc.upper() == wanted:
                return values[0].strip() if values and values[0].strip() else None
        return None

    def comments(self) -> list[tuple[str, str, str]]:
        """``(language, description, text)`` for every COMM frame."""
        return [_decode_comm(frame.data, frame.major) for frame in self._decodable("COMM")]

    def popm_rating(self) -> int | None:
        for frame in self._decodable("POPM"):
            _email, sep, rest = frame.data.partition(b"\x00")
            if sep and rest:
                return rest[0]
        return None

    def geob(self) -> list[tuple[str, str, str, bytes]]:
        """``(mime, filename, description, payload)`` for every GEOB frame."""
        return [_decode_geob(frame.data, frame.major) for frame in self._decodable("GEOB")]

    def pictures(self) -> list[tuple[str, int, str, bytes]]:
        """``(mime, picture_type, description, data)`` for every APIC frame."""
        return [_decode_apic(frame.data, frame.major) for frame in self._decodable("APIC")]

    # ----- mutation -----------------------------------------------------------
    def remove(self, predicate: Callable[[Frame], bool]) -> None:
        self.frames = [frame for frame in self.frames if not predicate(frame)]

    def set_text(self, frame_id: str, value: str) -> None:
        self.remove(lambda f: f.frame_id == frame_id)
        self.frames.append(Frame(frame_id, encode_text_frame(value, self.version)))

    def set_txxx(self, description: str, value: str) -> None:
        wanted = description.upper()
        self.remove(
            lambda f: f.frame_id == "TXXX" and not f.opaque and _decode_txxx(f.data, f.major)[0].upper() == wanted
        )
        self.frames.append(Frame("TXXX", encode_txxx(description, value, self.version)))

    def set_popm(self, email: str, rating: int, count: int = 0) -> None:
        self.remove(lambda f: f.frame_id == "POPM")
        self.frames.append(Frame("POPM", encode_popm(email, rating, count)))

    def set_geob(self, description: str, payload: bytes, mime: str = "application/octet-stream") -> None:
        self.remove(lambda f: f.frame_id == "GEOB" and not f.opaque and _decode_geob(f.data, f.major)[2] == description)
        self.frames.append(Frame("GEOB", encode_geob(mime, "", description, payload)))

    def _decodable(self, frame_id: str) -> Iterable[Frame]:
        return (frame for frame in self.frames if frame.frame_id == frame_id and not frame.opaque)


# ============================================================== read ========
def read_tag(path: Path) -> Id3Tag | None:
    """The ID3v2 tag at the start of ``path``, or ``None`` when it has none."""
    with path.open("rb") as handle:
        header = handle.read(HEADER_SIZE)
        _strict._reject_cut_short_header(path, header)
        if len(header) < HEADER_SIZE or header[:3] != b"ID3":
            return None
        major, flags = header[3], header[5]
        if any(b & 0x80 for b in header[6:10]):
            # The tag size is syncsafe in v2.3 and v2.4 alike. Masking the high bit would
            # read the declared body as fewer bytes, and the tag would import as empty.
            raise Id3Error(f"{path}: ID3 tag size bytes {header[6:10].hex()} are not syncsafe")
        body_size = _syncsafe_to_int(header[6:10])
        body = handle.read(body_size)
        has_footer = major == 4 and bool(flags & 0x10)
        footer_bytes = handle.read(FOOTER_SIZE) if has_footer else b""
    _strict._check_tag_header(path, header, body, footer_bytes)
    footer = FOOTER_SIZE if has_footer else 0
    tag = Id3Tag(version=major, original_size=HEADER_SIZE + body_size + footer)
    tag_unsync = bool(flags & 0x80)
    if major == 3 and tag_unsync:
        body = _undo_unsync(body)
    offset = _skip_extended_header(body, major) if flags & 0x40 else 0
    tag.frames, frames_end, unsynced = _parse_frames(body, offset, major, tag_unsync, path)
    _strict._verify_extended_header(body, major, flags, offset, frames_end, unsynced, path)
    _strict._check_text_encodings_at_read(tag.frames, major, path)
    _strict._check_footer_padding(path, body, frames_end, has_footer)
    return tag


def _sealed_mask(major: int) -> int:
    if major == 4:
        return _V24_COMPRESSED | _V24_ENCRYPTED
    if major == 3:
        return _V23_COMPRESSED | _V23_ENCRYPTED
    return _V24_COMPRESSED | _V24_ENCRYPTED | _V23_COMPRESSED | _V23_ENCRYPTED


def _parse_frames(body: bytes, offset: int, major: int, tag_unsync: bool, path: Path) -> tuple[list[Frame], int, bool]:
    """The frames in ``body`` from ``offset``, the offset where they end (the padding starts there), and whether
    any v2.4 frame is unsynchronised."""
    frames: list[Frame] = []
    unsynced = False
    while offset + HEADER_SIZE <= len(body):
        frame_id_raw = body[offset : offset + 4]
        if frame_id_raw[0] == 0:  # padding, checked below for non-zero bytes after it
            break
        if not all(48 <= b <= 57 or 65 <= b <= 90 for b in frame_id_raw):
            raise Id3Error(f"{path}: invalid frame id {frame_id_raw!r} at tag offset {offset}")
        size_bytes = body[offset + 4 : offset + 8]
        size = _frame_size(size_bytes, major, body, offset)
        status, fmt = body[offset + 8], body[offset + 9]
        start = offset + HEADER_SIZE
        end = start + size
        if size == 0:
            raise Id3Error(
                f"{path}: frame {frame_id_raw.decode()} has no data bytes, but the spec requires at least one"
            )
        if end > len(body):
            raise Id3Error(f"{path}: frame {frame_id_raw.decode()} runs past the end of the tag")
        frame_id = frame_id_raw.decode("ascii")
        _check_reserved_frame_flags(path, frame_id, major, status, fmt)
        unsynced = unsynced or (major == 4 and bool(fmt & _V24_UNSYNC))
        group_id, data, fmt = _unwrap_frame_data(body[start:end], fmt, major, tag_unsync, f"{path}: frame {frame_id}")
        frames.append(Frame(frame_id, data, status, fmt, major=major, group_id=group_id))
        offset = end
    if any(body[offset:]):
        # Fewer bytes than a frame header are left, and they are not padding: the tag is cut short.
        raise Id3Error(
            f"{path}: {len(body) - offset} trailing bytes at tag offset {offset} are not a whole frame header"
        )
    return frames, offset, unsynced


def _check_reserved_frame_flags(path: Path, frame_id: str, major: int, status: int, fmt: int) -> None:
    """Reject a frame that sets a status or format flag bit the spec leaves reserved."""
    reserved_status = status & _RESERVED_STATUS_FLAGS[major]
    if reserved_status:
        raise Id3Error(f"{path}: frame {frame_id} sets reserved status flag bits 0x{reserved_status:02x}")
    reserved_format = fmt & _RESERVED_FORMAT_FLAGS[major]
    if reserved_format:
        raise Id3Error(f"{path}: frame {frame_id} sets reserved format flag bits 0x{reserved_format:02x}")


def _unwrap_frame_data(
    data: bytes, fmt: int, major: int, tag_unsync: bool, where: str
) -> tuple[int | None, bytes, int]:
    """Split off a group byte, then undo v2.4's length indicator and
    unsynchronisation, for a frame that is not sealed. Returns the group id,
    the frame body, and the format flags left to keep."""
    if fmt & _sealed_mask(major):
        return None, data, fmt
    if major == 4 and (fmt & _V24_UNSYNC or tag_unsync):
        # v2.4 unsynchronisation covers every byte after the frame header,
        # the group byte and length indicator included, so it is undone first.
        data = _undo_unsync(data)
        fmt &= ~_V24_UNSYNC
    group_id = None
    if fmt & (_V24_GROUPED if major == 4 else _V23_GROUPED):
        # The group byte comes first in an unsealed frame's data.
        if not data:
            raise Id3Error(f"{where} is grouped but has no group byte")
        group_id, data = data[0], data[1:]
    if major == 4 and fmt & _V24_DATA_LENGTH:
        data = _strip_data_length_indicator(data, where)
        fmt &= ~_V24_DATA_LENGTH
    return group_id, data, fmt


def _strip_data_length_indicator(data: bytes, where: str) -> bytes:
    """Remove a v2.4 data-length indicator, which must be syncsafe and must match the bytes after it."""
    if len(data) < 4:
        raise Id3Error(f"{where} sets the data-length indicator but holds only {len(data)} bytes")
    indicator = data[:4]
    if any(b & 0x80 for b in indicator):
        raise Id3Error(f"{where} data-length indicator {indicator.hex()} is not syncsafe")
    declared = _syncsafe_to_int(indicator)
    if declared != len(data) - 4:
        raise Id3Error(f"{where} data-length indicator declares {declared} bytes but the frame holds {len(data) - 4}")
    return data[4:]


def _frame_size(size_bytes: bytes, major: int, body: bytes, offset: int) -> int:
    plain = struct.unpack(">I", size_bytes)[0]
    if major == 3:
        return plain
    # v2.4 sizes are syncsafe; some writers (old iTunes) wrote plain ints.
    # A byte with the high bit set cannot be syncsafe, so it is plain.
    if any(b & 0x80 for b in size_bytes):
        return plain
    syncsafe = _syncsafe_to_int(size_bytes)
    if syncsafe == plain or _next_frame_plausible(body, offset + HEADER_SIZE + syncsafe):
        return syncsafe
    if _next_frame_plausible(body, offset + HEADER_SIZE + plain):
        return plain
    return syncsafe


def _next_frame_plausible(body: bytes, at: int) -> bool:
    if at == len(body):
        return True
    if at > len(body) or at + 4 > len(body):
        return False
    chunk = body[at : at + 4]
    return chunk[0] == 0 or all(48 <= b <= 57 or 65 <= b <= 90 for b in chunk)


def _skip_extended_header(body: bytes, major: int) -> int:
    if len(body) < 4:
        raise Id3Error("ID3 extended header is truncated: fewer than four body bytes")
    if major == 3:
        size = struct.unpack(">I", body[:4])[0]
        end = 4 + size
    else:
        if any(b & 0x80 for b in body[:4]):
            raise Id3Error(f"ID3 extended header size bytes {body[:4].hex()} are not syncsafe")
        size = _syncsafe_to_int(body[:4])
        end = size
    if end > len(body):
        # Frames would start past the end, so the tag would read as empty with no loss counted.
        raise Id3Error(f"ID3 extended header declares {end} bytes but the tag body holds {len(body)}")
    if major == 3:
        _strict._check_v23_extended_header(body, size)
    else:
        # v2.4 counts the size field too, and its one flag-count byte must read 1.
        if end < 6:
            raise Id3Error(f"ID3 extended header declares {end} bytes, fewer than the 6 a v2.4 header needs")
        if body[4] != 1:
            raise Id3Error(f"ID3 extended header has {body[4]} flag bytes, but a v2.4 header has 1")
        _strict._check_v24_extended_payloads(body, end)
    return end


# ============================================================== write =======
def render(tag: Id3Tag, *, padding: int) -> bytes:
    """The tag's on-disk bytes: header, frames, ``padding`` zero bytes."""
    frames = b"".join(_render_frame(frame, tag.version) for frame in tag.frames)
    body = frames + b"\x00" * padding
    return b"ID3" + bytes([tag.version, 0, 0]) + _int_to_syncsafe(len(body)) + body


def save(path: Path, tag: Id3Tag) -> None:
    """Write ``tag`` to the front of ``path``, replacing any existing ID3v2 tag.

    The new file is assembled beside the original and swapped in with
    ``os.replace``, so a crash leaves either the old file or the new one.
    """
    frames_size = len(render(tag, padding=0))
    padding = tag.original_size - frames_size if tag.original_size >= frames_size else DEFAULT_PADDING
    tag_bytes = render(tag, padding=padding)
    existing = read_tag(path)
    audio_offset = existing.original_size if existing is not None else 0
    replace_head(path, tag_bytes, audio_offset)


def load_or_new(path: Path, *, new_version: int = NEW_TAG_VERSION_DEFAULT) -> Id3Tag:
    """The file's tag, or an empty one in ``new_version`` when it has none."""
    existing = read_tag(path)
    return existing if existing is not None else Id3Tag(version=new_version)


# ============================================================ encoders ======
# ============================================================ decoders ======
def _check_text_encoding(encoding: int, major: int) -> None:
    """Reject a text encoding byte that the frame's version does not define. v2.3 has only 0 and 1; v2.4 adds 2 and 3.

    ``major`` is 0 for a frame built in memory, which is not checked against a version.
    """
    if major == 3 and encoding > _ENC_UTF16:
        raise Id3Error(f"ID3v2.3 frame uses text encoding byte {encoding}, which only v2.4 defines")
    if major == 4 and encoding > _ENC_UTF8:
        raise Id3Error(f"ID3v2.4 frame uses text encoding byte {encoding}, which the spec does not define")


def _decode_text_values(data: bytes, major: int = 0, errors: str = "replace") -> list[str]:
    if not data:
        return []
    encoding, raw = data[0], data[1:]
    _check_text_encoding(encoding, major)
    return [v for v in _decode_string(raw, encoding, errors).split("\x00") if v != ""] if raw else []


def _malformed_as_id3_error(decode):
    """A decoder that indexes past a short or NUL-less body raises Id3Error, not IndexError.

    A frame a tag writer damaged is an unreadable tag, which callers count as a
    loss; an IndexError would escape them and stop the whole read.
    """

    @functools.wraps(decode)
    def checked(data: bytes, major: int = 0, errors: str = "replace"):
        try:
            return decode(data, major, errors)
        except (IndexError, ValueError) as exc:
            raise Id3Error(f"malformed {decode.__name__.removeprefix('_decode_')} frame body") from exc

    return checked


@_malformed_as_id3_error
def _decode_txxx(data: bytes, major: int = 0, errors: str = "replace") -> tuple[str, list[str]]:
    encoding = data[0]
    _check_text_encoding(encoding, major)
    order = _ByteOrder()
    desc_raw, rest = _split_description(data[1:], encoding, errors)
    description = _decode_string(desc_raw, encoding, errors, order)
    values = [v for v in _decode_string(rest, encoding, errors, order).split("\x00") if v != ""]
    return description, values


@_malformed_as_id3_error
def _decode_comm(data: bytes, major: int = 0, errors: str = "replace") -> tuple[str, str, str]:
    encoding = data[0]
    _check_text_encoding(encoding, major)
    language_raw = data[1:4]
    if errors == "strict" and not (len(language_raw) == 3 and language_raw.isascii() and language_raw.isalpha()):
        # The language is an ISO-639-2 code: three ASCII letters, with "XXX" for unknown. Anything else is damage.
        raise UnicodeDecodeError("ascii", language_raw, 0, len(language_raw), "COMM language is not an ISO-639-2 code")
    language = language_raw.decode("latin-1", errors="replace")
    order = _ByteOrder()
    desc_raw, text_raw = _split_description(data[4:], encoding, errors)
    description = _decode_string(desc_raw, encoding, errors, order)
    return language, description, _decode_string(text_raw, encoding, errors, order).rstrip("\x00")


@_malformed_as_id3_error
def _decode_geob(data: bytes, major: int = 0, errors: str = "replace") -> tuple[str, str, str, bytes]:
    encoding = data[0]
    _check_text_encoding(encoding, major)
    mime_raw, rest = data[1:].split(b"\x00", 1)
    filename_raw, rest = _split_required_terminator(rest, encoding)
    desc_raw, payload = _split_required_terminator(rest, encoding)
    order = _ByteOrder()
    return (
        mime_raw.decode("latin-1"),
        _decode_string(filename_raw, encoding, errors, order),
        _decode_string(desc_raw, encoding, errors, order),
        payload,
    )


@_malformed_as_id3_error
def _decode_apic(data: bytes, major: int = 0, errors: str = "replace") -> tuple[str, int, str, bytes]:
    encoding = data[0]
    _check_text_encoding(encoding, major)
    mime_raw, rest = data[1:].split(b"\x00", 1)
    picture_type = rest[0]
    desc_raw, image = _split_description(rest[1:], encoding, errors)
    return mime_raw.decode("latin-1"), picture_type, _decode_string(desc_raw, encoding, errors), image


# ============================================================ strings =======
class _ByteOrder:
    """The UTF-16 byte order a frame's strings share. The first byte-order mark in the frame sets it."""

    def __init__(self) -> None:
        self.codec: str | None = None


def _decode_string(raw: bytes, encoding: int, errors: str = "replace", order: _ByteOrder | None = None) -> str:
    if encoding == _ENC_LATIN1:
        return raw.decode("latin-1")
    if encoding == _ENC_UTF16:
        return _decode_utf16_with_bom(raw, errors, order or _ByteOrder())
    if encoding == _ENC_UTF16BE:
        # Encoding 2 is UTF-16BE without a byte-order mark, in every NUL-separated value, so a strict read rejects one.
        values = _split_utf16(raw)
        if errors == "strict" and any(v[:2] in (b"\xfe\xff", b"\xff\xfe") for v in values):
            raise UnicodeDecodeError("utf-16-be", raw, 0, 2, "byte-order mark in UTF-16BE text")
        return "\x00".join(v.decode("utf-16-be", errors=errors) for v in values)
    if encoding == _ENC_UTF8:
        return raw.decode("utf-8", errors=errors)
    raise Id3Error(f"unknown ID3 text encoding byte {encoding}")


def _decode_utf16_with_bom(raw: bytes, errors: str, order: _ByteOrder) -> str:
    # v2.4 allows a BOM per null-separated value, and v2.3 requires one. Every string in a frame shares the byte order
    # the first BOM in the frame set, so a later BOM that disagrees is a conflict, not a new byte order.
    parts = []
    for chunk in _split_utf16(raw):
        text = chunk
        if chunk[:2] in (b"\xff\xfe", b"\xfe\xff"):
            bom_codec = "utf-16-le" if chunk[:2] == b"\xff\xfe" else "utf-16-be"
            if order.codec is not None and bom_codec != order.codec and errors == "strict":
                raise UnicodeDecodeError("utf-16", chunk, 0, 2, "conflicting byte-order mark")
            order.codec = order.codec or bom_codec
            text = chunk[2:]
        elif errors == "strict" and order.codec is None:
            # An empty first field has no BOM either, and the byte order is still unknown, so it is malformed too.
            raise UnicodeDecodeError("utf-16", chunk, 0, min(2, len(chunk)), "missing byte-order mark")
        parts.append(text.decode(order.codec or "utf-16-le", errors=errors))
    return "\x00".join(parts)


def _split_utf16(raw: bytes) -> list[bytes]:
    chunks, start = [], 0
    for i in range(0, len(raw) - 1, 2):
        if raw[i : i + 2] == b"\x00\x00":
            chunks.append(raw[start:i])
            start = i + 2
    chunks.append(raw[start:])
    return chunks


def _terminator(encoding: int) -> bytes:
    return b"\x00\x00" if encoding in (_ENC_UTF16, _ENC_UTF16BE) else b"\x00"


def _split_terminated(raw: bytes, encoding: int) -> tuple[bytes, bytes]:
    if encoding in (_ENC_UTF16, _ENC_UTF16BE):
        for i in range(0, len(raw) - 1, 2):
            if raw[i : i + 2] == b"\x00\x00":
                return raw[:i], raw[i + 2 :]
        return raw, b""
    head, _sep, tail = raw.partition(b"\x00")
    return head, tail


def _split_description(raw: bytes, encoding: int, errors: str) -> tuple[bytes, bytes]:
    """Split a description off its terminator. A strict read requires the terminator; a lenient read keeps the old split."""
    if errors == "strict":
        return _split_required_terminator(raw, encoding)
    return _split_terminated(raw, encoding)


def _split_required_terminator(raw: bytes, encoding: int) -> tuple[bytes, bytes]:
    """Split at the encoding's terminator, raising when the frame has none.

    _split_terminated returns the whole input when no terminator is present, which
    would let a damaged GEOB field swallow the payload. A GEOB field must end with
    its terminator, so a missing one raises ValueError for the decoder to report.
    """
    head, tail = _split_terminated(raw, encoding)
    terminator_len = 2 if encoding in (_ENC_UTF16, _ENC_UTF16BE) else 1
    if len(head) + terminator_len > len(raw):
        raise ValueError("frame field has no terminator")
    return head, tail


# ============================================================ integers ======
def _syncsafe_to_int(raw: bytes) -> int:
    value = 0
    for byte in raw:
        value = (value << 7) | (byte & 0x7F)
    return value


def _int_to_syncsafe(value: int) -> bytes:
    if value >= 1 << 28:
        raise Id3Error(f"ID3 size {value} exceeds the 256 MB syncsafe limit")
    return bytes([(value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F])


def _undo_unsync(data: bytes) -> bytes:
    return data.replace(b"\xff\x00", b"\xff")


__all__ = [
    "DEFAULT_PADDING",
    "Frame",
    "Id3Error",
    "Id3Tag",
    "encode_apic",
    "encode_comm",
    "encode_geob",
    "encode_popm",
    "encode_text_frame",
    "encode_txxx",
    "load_or_new",
    "read_tag",
    "render",
    "save",
]


# The strict checks import this module's decoders, so they load last, after every name above exists.
from apps.shared import id3v2_strict as _strict  # noqa: E402
from apps.shared.id3v2_write import (  # noqa: E402
    _render_frame,
    encode_apic,
    encode_comm,
    encode_geob,
    encode_popm,
    encode_text_frame,
    encode_txxx,
)
