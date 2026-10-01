"""Rewrite an audio file's metadata atomically (temp file + ``os.replace``).

Shared by the in-house tag writers (:mod:`apps.shared.id3v2`,
:mod:`apps.shared.flac_meta`, :mod:`apps.shared.mp4_meta`,
:mod:`apps.shared.ogg_comment`): each replaces a metadata region of a file
whose audio bytes must come through untouched.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

COPY_CHUNK = 1 << 20


def rewrite_atomic(path: Path, build: Callable[[BinaryIO, BinaryIO], None]) -> None:
    """Rebuild ``path`` by calling ``build(src, out)`` and swap the result in.

    The new file is built beside the original, fsynced, given the original's
    permission bits, then swapped in with ``os.replace``: a crash leaves the old
    file or the new one, never a half-written mix. A failure removes the temp
    file and re-raises.
    """
    mode = path.stat().st_mode
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tag-", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, path.open("rb") as src:
            build(src, out)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def copy_rest(src: BinaryIO, out: BinaryIO, offset: int) -> None:
    """Copy ``src`` from ``offset`` to its end into ``out``, unchanged."""
    src.seek(offset)
    while chunk := src.read(COPY_CHUNK):
        out.write(chunk)


def copy_range(src: BinaryIO, out: BinaryIO, start: int, end: int) -> None:
    """Copy ``src`` bytes ``[start, end)`` into ``out``; a short source is an error."""
    src.seek(start)
    remaining = end - start
    while remaining:
        chunk = src.read(min(COPY_CHUNK, remaining))
        if not chunk:
            raise EOFError(f"source ends {remaining} bytes before offset {end}")
        out.write(chunk)
        remaining -= len(chunk)


def replace_range(path: Path, start: int, end: int, replacement: bytes) -> None:
    """Rewrite ``path`` with its bytes ``[start, end)`` replaced by ``replacement``."""

    def build(src: BinaryIO, out: BinaryIO) -> None:
        copy_range(src, out, 0, start)
        out.write(replacement)
        copy_rest(src, out, end)

    rewrite_atomic(path, build)


def replace_head(path: Path, head: bytes, audio_offset: int) -> None:
    """Rewrite ``path`` as ``head`` + its own bytes from ``audio_offset`` on."""
    replace_range(path, 0, audio_offset, head)


__all__ = ["copy_range", "copy_rest", "replace_head", "replace_range", "rewrite_atomic"]
