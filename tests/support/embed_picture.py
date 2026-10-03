"""Stdlib picture embeds for reader tests. Not a product tag writer.

mutagen is GPL and is not a product dependency. These helpers build the
bytes tinytag already reads: an ID3v2.3 APIC tag, a RIFF ``id3 `` chunk,
and a FLAC METADATA_BLOCK_PICTURE.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def syncsafe(n: int) -> bytes:
    return bytes(((n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F))


def apic_frame(image: bytes, *, mime: str, desc: str = "cover", picture_type: int = 3) -> bytes:
    body = (
        b"\x00"
        + mime.encode("latin-1")
        + b"\x00"
        + bytes((picture_type,))
        + desc.encode("latin-1")
        + b"\x00"
        + image
    )
    return b"APIC" + len(body).to_bytes(4, "big") + b"\x00\x00" + body


def id3_tag(frames: list[bytes]) -> bytes:
    payload = b"".join(frames)
    return b"ID3\x03\x00\x00" + syncsafe(len(payload)) + payload


def prepend_id3(audio: bytes, frames: list[bytes]) -> bytes:
    return id3_tag(frames) + audio


def with_id3_apic(
    audio: bytes,
    image: bytes,
    *,
    mime: str = "image/jpeg",
    desc: str = "cover",
    picture_type: int = 3,
) -> bytes:
    return prepend_id3(
        audio,
        [apic_frame(image, mime=mime, desc=desc, picture_type=picture_type)],
    )


def insert_wav_id3(wav: bytes, frames: list[bytes]) -> bytes:
    """Insert an ``id3 `` chunk immediately after the WAVE marker."""
    if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    tag = id3_tag(frames)
    if len(tag) % 2:
        tag += b"\x00"
    chunk = b"id3 " + len(tag).to_bytes(4, "little") + tag
    out = wav[:12] + chunk + wav[12:]
    return out[:4] + (len(out) - 8).to_bytes(4, "little") + out[8:]


def flac_picture_block(data: bytes, *, mime: str, picture_type: int = 3, last: bool = True) -> bytes:
    mime_b = mime.encode("ascii")
    desc = b"cover"
    body = (
        picture_type.to_bytes(4, "big")
        + len(mime_b).to_bytes(4, "big")
        + mime_b
        + len(desc).to_bytes(4, "big")
        + desc
        + b"\x00\x00\x00\x00" * 4
        + len(data).to_bytes(4, "big")
        + data
    )
    kind = 6 | (0x80 if last else 0)
    return bytes((kind,)) + len(body).to_bytes(3, "big") + body


def insert_flac_picture(flac: bytes, data: bytes, *, mime: str) -> bytes:
    if flac[:4] != b"fLaC":
        raise ValueError("not a FLAC file")
    i = 4
    out = bytearray(b"fLaC")
    while i + 4 <= len(flac):
        header = flac[i]
        is_last = bool(header & 0x80)
        size = int.from_bytes(flac[i + 1 : i + 4], "big")
        block = flac[i : i + 4 + size]
        i += 4 + size
        if is_last:
            out += bytes((header & 0x7F,)) + block[1:]
            out += flac_picture_block(data, mime=mime, last=True)
            out += flac[i:]
            return bytes(out)
        out += block
    raise ValueError("FLAC has no last metadata block")


def embed_m4a_cover(src: Path, dst: Path, image: bytes) -> None:
    """Attach ``image`` as an MP4 cover via ffmpeg stream copy."""
    cover = dst.with_suffix(".cover.jpg")
    cover.write_bytes(image)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
            "-i", str(cover),
            "-map", "0",
            "-map", "1",
            "-c", "copy",
            "-disposition:v:0", "attached_pic",
            str(dst),
        ],
        check=True,
    )
