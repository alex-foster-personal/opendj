from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from apps.webui.frontend.tests.e2e.support.deckload_fixture_core import _uv
from apps.webui.frontend.tests.e2e.support.deckload_fixture_model import (
    ARTWORK_PNG_SHA256,
    ARTWORK_SOURCE_PNG,
    REPOSITORY_ROOT,
    RESCUE_PLAYBACK_ARTWORK_TRACK,
)

_WAV_ID3_CHUNK_IDS = (b"id3 ", b"ID3 ")


def _wav_chunks(data: bytes, path: Path) -> list[tuple[bytes, bytes]]:
    """The RIFF/WAVE file's top-level ``(chunk_id, body)`` pairs, in order."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise SystemExit(f"[ERROR] not a RIFF/WAVE file: {path}")
    chunks: list[tuple[bytes, bytes]] = []
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        body = data[offset + 8 : offset + 8 + size]
        if len(body) != size:
            raise SystemExit(f"[ERROR] truncated {chunk_id!r} chunk in {path}")
        chunks.append((chunk_id, body))
        offset += 8 + size + (size & 1)
    return chunks


def wav_id3_pictures(wav_path: Path) -> list[tuple[str, int, str, bytes]]:
    """``(mime, picture_type, description, data)`` of every APIC frame in the
    WAV's ID3 chunk; empty when the file has no ID3 chunk."""
    from apps.shared import id3v2

    bodies = [body for chunk_id, body in _wav_chunks(wav_path.read_bytes(), wav_path) if chunk_id in _WAV_ID3_CHUNK_IDS]
    if not bodies:
        return []
    if len(bodies) != 1:
        raise SystemExit(f"[ERROR] {wav_path} holds {len(bodies)} ID3 chunks, expected one")
    tag_path = wav_path.with_name(wav_path.name + ".id3-probe")
    try:
        tag_path.write_bytes(bodies[0])
        tag = id3v2.read_tag(tag_path)
    finally:
        tag_path.unlink(missing_ok=True)
    if tag is None:
        raise SystemExit(f"[ERROR] {wav_path} has an ID3 chunk that is not an ID3v2 tag")
    return tag.pictures()


def _assert_single_front_cover_apic(wav_path: Path) -> None:
    pictures = wav_id3_pictures(wav_path)
    if len(pictures) != 1:
        raise SystemExit(f"[ERROR] expected exactly one APIC frame, got {len(pictures)}")
    mime, picture_type, _desc, _data = pictures[0]
    if picture_type != 3 or mime != "image/png":
        raise SystemExit(f"[ERROR] expected front-cover PNG APIC, got type={picture_type} mime={mime}")


def _write_wav_cover(wav_path: Path, png_bytes: bytes) -> None:
    """Replace the WAV's ID3 chunk with one holding a single front-cover PNG.

    Written with the project's own ID3v2 encoder (apps.shared.id3v2), since
    the GPL mutagen it replaced is not a dependency.
    """
    from apps.shared import id3v2

    data = wav_path.read_bytes()
    kept = [(cid, body) for cid, body in _wav_chunks(data, wav_path) if cid not in _WAV_ID3_CHUNK_IDS]
    tag = id3v2.Id3Tag(version=4)
    tag.frames.append(id3v2.Frame("APIC", id3v2.encode_apic("image/png", 3, "cover", png_bytes)))
    kept.append((b"id3 ", id3v2.render(tag, padding=0)))
    payload = b"WAVE" + b"".join(
        cid + len(body).to_bytes(4, "little") + body + (b"\x00" if len(body) & 1 else b"")
        for cid, body in kept
    )
    tmp = wav_path.with_name(wav_path.name + ".tmp")
    tmp.write_bytes(b"RIFF" + len(payload).to_bytes(4, "little") + payload)
    os.replace(tmp, wav_path)


_RESCUE_ARTWORK_EMBED_SCRIPT = """
import hashlib
import sys
from pathlib import Path

from apps.shared import audio_files
from tests.support.embed_picture import apic_frame, insert_wav_id3

wav_path = Path(sys.argv[1])
png_path = Path(sys.argv[2])
expected_sha = sys.argv[3]
png_bytes = png_path.read_bytes()
if hashlib.sha256(png_bytes).hexdigest() != expected_sha:
    raise SystemExit("[ERROR] artwork PNG checksum mismatch in embed worker")
if audio_files.read_embedded_artwork(wav_path) == (png_bytes, "image/png"):
    raise SystemExit(0)
raw = wav_path.read_bytes()
# Drop a previous id3 chunk by rewriting from the original WAVE body is not
# required: the caller only embeds when the reader does not already match.
if raw[8:12] != b"WAVE":
    raise SystemExit("[ERROR] rescue artwork file is not a WAV")
frame = apic_frame(png_bytes, mime="image/png")
wav_path.write_bytes(insert_wav_id3(raw, [frame]))
if audio_files.read_embedded_artwork(wav_path) != (png_bytes, "image/png"):
    raise SystemExit("[ERROR] embedded rescue artwork did not round-trip")
"""


def _ensure_rescue_artwork_embedded(audio_dir: Path) -> None:
    """Embed the checked-in PNG on the designated browse-only rescue WAV (idempotent)."""
    wav_path = audio_dir / RESCUE_PLAYBACK_ARTWORK_TRACK.filename
    if not wav_path.is_file():
        raise SystemExit(f"[ERROR] rescue artwork wav missing: {wav_path}")
    png_bytes = ARTWORK_SOURCE_PNG.read_bytes()
    if png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"[ERROR] artwork source is not a PNG: {ARTWORK_SOURCE_PNG}")
    digest = hashlib.sha256(png_bytes).hexdigest()
    if digest != ARTWORK_PNG_SHA256:
        raise SystemExit(
            f"[ERROR] artwork PNG checksum mismatch for {ARTWORK_SOURCE_PNG}: {digest}"
        )
    command = [
        _uv(),
        "run",
        "--no-sync",
        "python",
        "-c",
        _RESCUE_ARTWORK_EMBED_SCRIPT,
        str(wav_path),
        str(ARTWORK_SOURCE_PNG),
        ARTWORK_PNG_SHA256,
    ]
    result = subprocess.run(
        command, cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"[ERROR] rescue artwork embed failed with exit code {result.returncode}"
        )

