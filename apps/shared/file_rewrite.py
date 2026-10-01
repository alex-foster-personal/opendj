"""Swap a file's leading metadata bytes atomically (temp file + ``os.replace``).

Shared by the in-house tag writers (:mod:`apps.shared.id3v2`,
:mod:`apps.shared.flac_meta`): both replace a header region in front of audio
bytes that must come through untouched.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def replace_head(path: Path, head: bytes, audio_offset: int) -> None:
    """Rewrite ``path`` as ``head`` + its own bytes from ``audio_offset`` on.

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
            out.write(head)
            src.seek(audio_offset)
            while chunk := src.read(1 << 20):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


__all__ = ["replace_head"]
