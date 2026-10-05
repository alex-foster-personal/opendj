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

import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from apps.shared.file_rewrite import replace_head

HEADER_SIZE = 10
DEFAULT_PADDING = 2048
NEW_TAG_VERSION_DEFAULT = 3
"""v2.3 is what Serato DJ Pro writes and what every DJ app reads."""

# Frame format-flag bits (second flag byte).
_V23_COMPRESSED = 0x80
_V23_ENCRYPTED = 0x40
_V24_COMPRESSED = 0x08
_V24_ENCRYPTED = 0x04
_V24_UNSYNC = 0x02
_V24_DATA_LENGTH = 0x01

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

    @property
    def opaque(self) -> bool:
        """Compressed or encrypted: kept verbatim, never decoded."""
        return bool(self.format_flags & (_V24_COMPRESSED | _V24_ENCRYPTED | _V23_COMPRESSED | _V23_ENCRYPTED))


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
                return _decode_text_values(frame.data)
        return []

    def first_text(self, frame_id: str) -> str | None:
        values = [v for v in self.text(frame_id) if v.strip()]
        return values[0].strip() if values else None

    def txxx(self, description: str) -> str | None:
        wanted = description.upper()
        for frame in self._decodable("TXXX"):
            desc, values = _decode_txxx(frame.data)
            if desc.upper() == wanted:
                return values[0].strip() if values and values[0].strip() else None
        return None

    def comments(self) -> list[tuple[str, str, str]]:
        """``(language, description, text)`` for every COMM frame."""
        return [_decode_comm(frame.data) for frame in self._decodable("COMM")]

    def popm_rating(self) -> int | None:
        for frame in self._decodable("POPM"):
            _email, sep, rest = frame.data.partition(b"\x00")
            if sep and rest:
                return rest[0]
        return None

    def geob(self) -> list[tuple[str, str, str, bytes]]:
        """``(mime, filename, description, payload)`` for every GEOB frame."""
        return [_decode_geob(frame.data) for frame in self._decodable("GEOB")]

    def pictures(self) -> list[tuple[str, int, str, bytes]]:
        """``(mime, picture_type, description, data)`` for every APIC frame."""
        return [_decode_apic(frame.data) for frame in self._decodable("APIC")]

    # ----- mutation -----------------------------------------------------------
    def remove(self, predicate: Callable[[Frame], bool]) -> None:
        self.frames = [frame for frame in self.frames if not predicate(frame)]

    def set_text(self, frame_id: str, value: str) -> None:
        self.remove(lambda f: f.frame_id == frame_id)
        self.frames.append(Frame(frame_id, encode_text_frame(value, self.version)))

    def set_txxx(self, description: str, value: str) -> None:
        wanted = description.upper()
        self.remove(lambda f: f.frame_id == "TXXX" and not f.opaque and _decode_txxx(f.data)[0].upper() == wanted)
        self.frames.append(Frame("TXXX", encode_txxx(description, value, self.version)))

    def set_popm(self, email: str, rating: int, count: int = 0) -> None:
        self.remove(lambda f: f.frame_id == "POPM")
        self.frames.append(Frame("POPM", encode_popm(email, rating, count)))

    def set_geob(self, description: str, payload: bytes, mime: str = "application/octet-stream") -> None:
        self.remove(lambda f: f.frame_id == "GEOB" and not f.opaque and _decode_geob(f.data)[2] == description)
        self.frames.append(Frame("GEOB", encode_geob(mime, "", description, payload)))

    def _decodable(self, frame_id: str) -> Iterable[Frame]:
        return (frame for frame in self.frames if frame.frame_id == frame_id and not frame.opaque)


# ============================================================== read ========
def read_tag(path: Path) -> Id3Tag | None:
    """The ID3v2 tag at the start of ``path``, or ``None`` when it has none."""
    with path.open("rb") as handle:
        header = handle.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE or header[:3] != b"ID3":
            return None
        major, _revision, flags = header[3], header[4], header[5]
        body_size = _syncsafe_to_int(header[6:10])
        body = handle.read(body_size)
    if major not in (3, 4):
        raise Id3Error(f"{path}: ID3v2.{major} tag is not supported (only v2.3 / v2.4)")
    if len(body) != body_size:
        raise Id3Error(f"{path}: ID3 tag claims {body_size} bytes but the file ends early")
    footer = 10 if (major == 4 and flags & 0x10) else 0
    tag = Id3Tag(version=major, original_size=HEADER_SIZE + body_size + footer)
    tag_unsync = bool(flags & 0x80)
    if major == 3 and tag_unsync:
        body = _undo_unsync(body)
    offset = _skip_extended_header(body, major) if flags & 0x40 else 0
    tag.frames = list(_parse_frames(body, offset, major, tag_unsync, path))
    return tag


def _parse_frames(body: bytes, offset: int, major: int, tag_unsync: bool, path: Path) -> Iterable[Frame]:
    while offset + HEADER_SIZE <= len(body):
        frame_id_raw = body[offset : offset + 4]
        if frame_id_raw[0] == 0:  # padding
            return
        if not all(48 <= b <= 57 or 65 <= b <= 90 for b in frame_id_raw):
            raise Id3Error(f"{path}: invalid frame id {frame_id_raw!r} at tag offset {offset}")
        size_bytes = body[offset + 4 : offset + 8]
        size = _frame_size(size_bytes, major, body, offset)
        status, fmt = body[offset + 8], body[offset + 9]
        start = offset + HEADER_SIZE
        end = start + size
        if end > len(body):
            raise Id3Error(f"{path}: frame {frame_id_raw.decode()} runs past the end of the tag")
        data = body[start:end]
        if major == 4 and not (fmt & (_V24_COMPRESSED | _V24_ENCRYPTED)):
            if fmt & _V24_DATA_LENGTH:
                data = data[4:]
                fmt &= ~_V24_DATA_LENGTH
            if fmt & _V24_UNSYNC or tag_unsync:
                data = _undo_unsync(data)
                fmt &= ~_V24_UNSYNC
        yield Frame(frame_id_raw.decode("ascii"), data, status, fmt)
        offset = end


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
    if major == 3:
        return 4 + struct.unpack(">I", body[:4])[0]
    return _syncsafe_to_int(body[:4])


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


def _render_frame(frame: Frame, version: int) -> bytes:
    if version == 4:
        size = _int_to_syncsafe(len(frame.data))
    else:
        size = struct.pack(">I", len(frame.data))
    return frame.frame_id.encode("ascii") + size + bytes([frame.status_flags, frame.format_flags]) + frame.data



# ============================================================ encoders ======
def encode_text_frame(value: str, version: int) -> bytes:
    encoding, encoded = _encode_string(value, version)
    return bytes([encoding]) + encoded


def encode_txxx(description: str, value: str, version: int) -> bytes:
    encoding, desc = _encode_string(description, version, prefer=_pick_encoding(description + value, version))
    _, val = _encode_string(value, version, prefer=encoding)
    return bytes([encoding]) + desc + _terminator(encoding) + val


def encode_comm(language: str, description: str, text: str, version: int) -> bytes:
    encoding, desc = _encode_string(description, version, prefer=_pick_encoding(description + text, version))
    _, body = _encode_string(text, version, prefer=encoding)
    lang = language.encode("latin-1")[:3].ljust(3, b" ")
    return bytes([encoding]) + lang + desc + _terminator(encoding) + body


def encode_popm(email: str, rating: int, count: int = 0) -> bytes:
    if not 0 <= rating <= 255:
        raise Id3Error(f"POPM rating must be 0..255, got {rating}")
    return email.encode("latin-1") + b"\x00" + bytes([rating]) + struct.pack(">I", count)


def encode_geob(mime: str, filename: str, description: str, payload: bytes) -> bytes:
    # Serato writes encoding 0 (latin-1) with ASCII mime / description.
    return (
        bytes([_ENC_LATIN1])
        + mime.encode("latin-1") + b"\x00"
        + filename.encode("latin-1") + b"\x00"
        + description.encode("latin-1") + b"\x00"
        + payload
    )


def encode_apic(mime: str, picture_type: int, description: str, data: bytes) -> bytes:
    return (
        bytes([_ENC_LATIN1])
        + mime.encode("latin-1") + b"\x00"
        + bytes([picture_type])
        + description.encode("latin-1") + b"\x00"
        + data
    )


# ============================================================ decoders ======
def _decode_text_values(data: bytes) -> list[str]:
    if not data:
        return []
    encoding, raw = data[0], data[1:]
    return [v for v in _decode_string(raw, encoding).split("\x00") if v != ""] if raw else []


def _decode_txxx(data: bytes) -> tuple[str, list[str]]:
    encoding = data[0]
    desc_raw, rest = _split_terminated(data[1:], encoding)
    values = [v for v in _decode_string(rest, encoding).split("\x00") if v != ""]
    return _decode_string(desc_raw, encoding), values


def _decode_comm(data: bytes) -> tuple[str, str, str]:
    encoding = data[0]
    language = data[1:4].decode("latin-1", errors="replace")
    desc_raw, text_raw = _split_terminated(data[4:], encoding)
    return language, _decode_string(desc_raw, encoding), _decode_string(text_raw, encoding).rstrip("\x00")


def _decode_geob(data: bytes) -> tuple[str, str, str, bytes]:
    encoding = data[0]
    mime_raw, rest = data[1:].split(b"\x00", 1)
    filename_raw, rest = _split_terminated(rest, encoding)
    desc_raw, payload = _split_terminated(rest, encoding)
    return (
        mime_raw.decode("latin-1"),
        _decode_string(filename_raw, encoding),
        _decode_string(desc_raw, encoding),
        payload,
    )


def _decode_apic(data: bytes) -> tuple[str, int, str, bytes]:
    encoding = data[0]
    mime_raw, rest = data[1:].split(b"\x00", 1)
    picture_type = rest[0]
    desc_raw, image = _split_terminated(rest[1:], encoding)
    return mime_raw.decode("latin-1"), picture_type, _decode_string(desc_raw, encoding), image


# ============================================================ strings =======
def _pick_encoding(value: str, version: int) -> int:
    try:
        value.encode("latin-1")
        return _ENC_LATIN1
    except UnicodeEncodeError:
        return _ENC_UTF8 if version == 4 else _ENC_UTF16


def _encode_string(value: str, version: int, prefer: int | None = None) -> tuple[int, bytes]:
    encoding = prefer if prefer is not None else _pick_encoding(value, version)
    if encoding == _ENC_LATIN1:
        return encoding, value.encode("latin-1")
    if encoding == _ENC_UTF8:
        return encoding, value.encode("utf-8")
    return encoding, value.encode("utf-16")  # BOM included


def _decode_string(raw: bytes, encoding: int) -> str:
    if encoding == _ENC_LATIN1:
        return raw.decode("latin-1")
    if encoding == _ENC_UTF16:
        return _decode_utf16_with_bom(raw)
    if encoding == _ENC_UTF16BE:
        return raw.decode("utf-16-be", errors="replace")
    if encoding == _ENC_UTF8:
        return raw.decode("utf-8", errors="replace")
    raise Id3Error(f"unknown ID3 text encoding byte {encoding}")


def _decode_utf16_with_bom(raw: bytes) -> str:
    # v2.4 allows a BOM per null-separated value, so decode value by value.
    parts = []
    for chunk in _split_utf16(raw):
        if chunk[:2] in (b"\xff\xfe", b"\xfe\xff"):
            parts.append(chunk.decode("utf-16", errors="replace"))
        else:
            parts.append(chunk.decode("utf-16-le", errors="replace"))
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
