"""ID3v2.4 COMM watermark writer (pure stdlib).

Plan 12-01 Step 6 (Open Question 1: picked stdlib path to avoid a new
dep; eyed3 is GPL-3 and mutagen GPL-2 -- stdlib is the safest).

Writes a minimal ID3v2.4 tag with a single COMM frame tagged
``eng:legal``. The tag is prepended to an existing MP3 file
(no re-encoding; we only touch bytes before the first MPEG frame).

If the file already has an ID3v2 header we keep the user's tag
intact and append our frame to its frame list; on tag size overflow
we grow the existing tag's size field. This is defensive -- ffmpeg's
rolling segmenter writes fresh MP3s without ID3v2 by default.

Spec references: ID3v2.4.0-structure, ID3v2.4.0-frames (id3.org).
"""
from __future__ import annotations

from pathlib import Path

PRIVACY_NOTE_DEFAULT = (
    "personal-review-only; contains copyrighted audio; do not distribute"
)

_ID3_HEADER_SIZE = 10
_SYNC_SAFE_MAX = 0x0FFFFFFF  # 28-bit synchsafe ceiling


def _synchsafe(size: int) -> bytes:
    """Encode ``size`` as a 4-byte ID3v2 synchsafe integer."""
    if size < 0 or size > _SYNC_SAFE_MAX:
        raise ValueError(f"size {size} out of synchsafe range")
    return bytes(
        [
            (size >> 21) & 0x7F,
            (size >> 14) & 0x7F,
            (size >> 7) & 0x7F,
            size & 0x7F,
        ]
    )


def _decode_synchsafe(raw: bytes) -> int:
    """Decode a 4-byte synchsafe integer back to int."""
    if len(raw) != 4:
        raise ValueError("synchsafe int must be 4 bytes")
    return (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]


def build_comm_frame(
    privacy_note: str = PRIVACY_NOTE_DEFAULT,
    *,
    description: str = "legal",
    language: str = "eng",
) -> bytes:
    """Build a single ID3v2.4 COMM frame as raw bytes.

    Frame layout: ``[id=4][size=4 synchsafe][flags=2][body]``
    Body: ``[encoding=0x03 utf-8][lang=3][desc utf-8 \0][text utf-8]``.
    """
    if len(language) != 3 or not language.isascii():
        raise ValueError("language must be a 3-letter ASCII code")
    encoding = b"\x03"  # UTF-8
    lang = language.encode("ascii")
    desc = description.encode("utf-8") + b"\x00"
    text = privacy_note.encode("utf-8")
    body = encoding + lang + desc + text
    frame_id = b"COMM"
    frame_flags = b"\x00\x00"
    return frame_id + _synchsafe(len(body)) + frame_flags + body


def build_id3v24_tag(frames: bytes) -> bytes:
    """Wrap ``frames`` in an ID3v2.4 tag header."""
    header = b"ID3" + bytes([4, 0, 0]) + _synchsafe(len(frames))
    return header + frames


def _read_existing_tag(data: bytes) -> tuple[int, bytes] | None:
    """If ``data`` starts with an ID3v2 tag, return (tag_len, tag_bytes)."""
    if len(data) < _ID3_HEADER_SIZE or data[:3] != b"ID3":
        return None
    tag_size = _decode_synchsafe(data[6:10])
    total = _ID3_HEADER_SIZE + tag_size
    if total > len(data):
        return None
    return total, data[:total]


def stamp(
    mp3_path: Path,
    *,
    privacy_note: str = PRIVACY_NOTE_DEFAULT,
    description: str = "legal",
) -> Path:
    """Prepend (or append-to-existing) an ID3v2.4 COMM watermark on ``mp3_path``.

    Returns ``mp3_path`` on success. Idempotent: calling twice with the
    same note keeps the frame content stable (the tag does grow a
    little each call because we don't de-dup).
    """
    path = Path(mp3_path)
    data = path.read_bytes()
    frame = build_comm_frame(privacy_note, description=description)
    existing = _read_existing_tag(data)
    if existing is None:
        # No prior ID3v2; build a fresh tag and prepend.
        new_tag = build_id3v24_tag(frame)
        path.write_bytes(new_tag + data)
        return path
    total_existing, tag_bytes = existing
    # Strip the old header (first 10 bytes), get original frames,
    # append ours, rebuild header.
    old_frames = tag_bytes[_ID3_HEADER_SIZE:]
    new_tag = build_id3v24_tag(old_frames + frame)
    path.write_bytes(new_tag + data[total_existing:])
    return path


def read_comm_frames(mp3_path: Path) -> list[tuple[str, str, str]]:
    """Return ``(lang, description, text)`` for every COMM frame.

    Minimal reader used by tests. Only supports encoding 0x03 (UTF-8)
    and 0x00 (ISO-8859-1); good enough for our own writes.
    """
    data = Path(mp3_path).read_bytes()
    if data[:3] != b"ID3":
        return []
    tag_size = _decode_synchsafe(data[6:10])
    body = data[_ID3_HEADER_SIZE : _ID3_HEADER_SIZE + tag_size]
    out: list[tuple[str, str, str]] = []
    pos = 0
    while pos + 10 <= len(body):
        frame_id = body[pos : pos + 4]
        if frame_id == b"\x00\x00\x00\x00":
            break
        frame_size = _decode_synchsafe(body[pos + 4 : pos + 8])
        # skip flags (2 bytes)
        frame_body = body[pos + 10 : pos + 10 + frame_size]
        pos += 10 + frame_size
        if frame_id != b"COMM" or len(frame_body) < 5:
            continue
        encoding = frame_body[0]
        lang = frame_body[1:4].decode("ascii", errors="replace")
        rest = frame_body[4:]
        if encoding == 0x03:  # UTF-8
            null = rest.find(b"\x00")
            if null < 0:
                continue
            description = rest[:null].decode("utf-8", errors="replace")
            text = rest[null + 1 :].decode("utf-8", errors="replace")
        else:  # assume ISO-8859-1
            null = rest.find(b"\x00")
            if null < 0:
                continue
            description = rest[:null].decode("latin-1", errors="replace")
            text = rest[null + 1 :].decode("latin-1", errors="replace")
        out.append((lang, description, text))
    return out


__all__ = [
    "PRIVACY_NOTE_DEFAULT",
    "build_comm_frame",
    "build_id3v24_tag",
    "stamp",
    "read_comm_frames",
]
