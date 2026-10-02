"""Rewrite an audio file's metadata atomically (temp file + ``os.replace``).

Shared by the in-house tag writers (:mod:`apps.shared.id3v2`,
:mod:`apps.shared.flac_meta`, :mod:`apps.shared.mp4_meta`,
:mod:`apps.shared.ogg_comment`): each replaces a metadata region of a file
whose audio bytes must come through untouched.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

COPY_CHUNK = 1 << 20


def rewrite_atomic(path: Path, build: Callable[[BinaryIO, BinaryIO], None]) -> None:
    """Rebuild ``path`` by calling ``build(src, out)`` and swap the result in.

    The new file is built beside the original, fsynced, given the original's
    permission bits, extended attributes and ACL (``copy_extended_metadata``),
    then swapped in with ``os.replace``: a crash leaves the old
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
        copy_extended_metadata(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# copyfile(3) flags, <copyfile.h>: COPYFILE_ACL | COPYFILE_XATTR.
_COPYFILE_ACL_AND_XATTR = (1 << 0) | (1 << 2)
_XATTR_UNSUPPORTED = {errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}


def copy_extended_metadata(src: Path, dst: Path) -> None:
    """Carry ``src``'s extended attributes (and, on macOS, its ACL) onto ``dst``.

    The replace swaps in a brand-new inode, so without this a tag edit would
    silently drop Finder tags, quarantine flags and access-control entries:
    more than the writer promises to change. A failure raises, which aborts
    the rewrite and leaves the original untouched.

    Platform seam: macOS uses copyfile(3), which copies both; Linux copies
    each xattr (POSIX ACLs live in xattrs there); a filesystem without xattr
    support has none to lose. Windows has no xattr API in Python and its ACLs
    are inherited from the directory, so nothing is copied there.
    """
    if sys.platform == "darwin":
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        if libc.copyfile(os.fsencode(src), os.fsencode(dst), None, _COPYFILE_ACL_AND_XATTR) < 0:
            code = ctypes.get_errno()
            raise OSError(code, f"copyfile ACL/xattr from {src}: {os.strerror(code)}")
        return
    if not hasattr(os, "listxattr"):
        return
    try:
        names = os.listxattr(src)
    except OSError as exc:
        if exc.errno in _XATTR_UNSUPPORTED:
            return
        raise
    for name in names:
        os.setxattr(dst, name, os.getxattr(src, name))


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
