"""Cheap cache-identity fingerprint, not a full-file hash.

blake2b over the file size plus the first and last 64 KiB. A change
entirely in the unsampled middle of a file larger than 128 KiB can
collide; that is accepted for this token.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

WINDOW: int = 64 * 1024
DIGEST_SIZE: int = 16


def content_fp_from_bytes(data: bytes) -> str:
    size = len(data)
    hasher = hashlib.blake2b(digest_size=DIGEST_SIZE)
    hasher.update(size.to_bytes(8, "little"))
    if size <= WINDOW:
        hasher.update(data)
    else:
        hasher.update(data[:WINDOW])
        hasher.update(data[-WINDOW:])
    return hasher.hexdigest()


def content_fp_from_path(path: Path, size: int | None = None) -> str:
    if size is None:
        size = path.stat().st_size
    hasher = hashlib.blake2b(digest_size=DIGEST_SIZE)
    hasher.update(size.to_bytes(8, "little"))
    with path.open("rb") as fh:
        if size <= WINDOW:
            hasher.update(fh.read(size))
        else:
            hasher.update(fh.read(WINDOW))
            fh.seek(size - WINDOW)
            hasher.update(fh.read(WINDOW))
    return hasher.hexdigest()
