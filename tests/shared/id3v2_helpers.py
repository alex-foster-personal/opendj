"""Shared helpers for the test_id3v2 test modules (split from test_id3v2.py)."""

from __future__ import annotations

import struct
import subprocess
from pathlib import Path

from apps.shared import id3v2


def _tag(path: Path) -> id3v2.Id3Tag:
    tag = id3v2.read_tag(path)
    assert tag is not None, f"{path} has no ID3v2 tag"
    return tag


def _decodes(path: Path) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.returncode == 0 and not result.stderr.strip()


def _raw_tag(version: int, frames: bytes, flags: int = 0) -> bytes:
    size = len(frames)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3" + bytes([version, 0, flags]) + syncsafe + frames


def _v23_crc_tag(tmp_path: Path, crc: int) -> Path:
    frames = b"TIT2" + struct.pack(">I", 2) + b"\x00\x00" + b"\x00A"  # a v2.3 frame, latin-1 text "A"
    ext = struct.pack(">I", 10) + b"\x80\x00" + struct.pack(">I", 0) + struct.pack(">I", crc)  # CRC flag set
    mp3 = tmp_path / f"v23_crc_{crc:08x}.mp3"
    mp3.write_bytes(_raw_tag(3, ext + frames, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    return mp3


def _syncsafe5(value: int) -> bytes:
    return bytes([(value >> 28) & 0x0F, (value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F])


def _v24_crc_tag(tmp_path: Path, crc: int) -> Path:
    frames = b"TIT2" + b"\x00\x00\x00\x02" + b"\x00\x00" + b"\x00A"  # a v2.4 frame, latin-1 text "A"
    ext = b"\x00\x00\x00\x0c" + b"\x01" + b"\x20" + b"\x05" + _syncsafe5(crc)  # CRC flag set
    mp3 = tmp_path / f"v24_crc_{crc:08x}.mp3"
    mp3.write_bytes(_raw_tag(4, ext + frames, flags=0x40) + b"\xff\xfb\x90\x00" * 4)
    return mp3


def _geob_body(description: bytes) -> bytes:
    return b"\x00" + b"application/octet-stream\x00" + b"\x00" + description + b"\x00" + b"payload"
