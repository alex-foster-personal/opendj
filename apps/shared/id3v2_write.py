"""Serialize ID3v2 frames: the text, comment, popularimeter, object and picture encoders.

:mod:`apps.shared.id3v2` re-exports these names, so callers keep importing them from there. The module
imports the frame model from it, and loads after it has finished.
"""

from __future__ import annotations

import struct

from apps.shared.id3v2 import (
    _ENC_LATIN1,
    _ENC_UTF8,
    _ENC_UTF16,
    Frame,
    Id3Error,
    _int_to_syncsafe,
    _terminator,
)


def _render_frame(frame: Frame, version: int) -> bytes:
    data = frame.data if frame.group_id is None else bytes([frame.group_id]) + frame.data
    size = _int_to_syncsafe(len(data)) if version == 4 else struct.pack(">I", len(data))
    return frame.frame_id.encode("ascii") + size + bytes([frame.status_flags, frame.format_flags]) + data


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
