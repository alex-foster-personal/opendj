"""Atomically replace a file with text or bytes (temp write, fsync, ``os.replace``)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` so a reader sees the old file or the new one.

    Temp file in the same directory, fsync, then ``os.replace`` (atomic on one
    filesystem), then fsync the directory so the rename itself is durable.
    """
    _replace_atomic(path, text, "w", "utf-8")


def write_atomic_bytes(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data``, with the same guarantees as ``write_atomic``."""
    _replace_atomic(path, data, "wb", None)


def _replace_atomic(path: Path, payload: str | bytes, mode: str, encoding: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, mode, encoding=encoding) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
