"""Rewrite an audio file's metadata atomically (temp file + ``os.replace``).

Shared by the in-house tag writers (:mod:`apps.shared.id3v2`,
:mod:`apps.shared.flac_meta`, :mod:`apps.shared.mp4_meta`,
:mod:`apps.shared.ogg_comment`): each replaces a metadata region of a file
whose audio bytes must come through untouched.
"""
from __future__ import annotations

import ctypes
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
    owner and group (``keep_ownership``), permission bits, extended attributes
    and ACL (``copy_extended_metadata``), then swapped in with ``os.replace``:
    a crash leaves the old file or the new one, never a half-written mix. A
    failure removes the temp file and re-raises, leaving the original as it was.
    """
    original = path.stat()
    mode = original.st_mode
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tag-", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, path.open("rb") as src:
            build(src, out)
            out.flush()
            os.fsync(out.fileno())
        keep_ownership(original, tmp)  # before chmod: a chown may clear set-id bits
        os.chmod(tmp, mode & 0o7777)
        copy_extended_metadata(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def keep_ownership(original: os.stat_result, tmp: Path) -> None:
    """Give ``tmp`` the original file's owner and group.

    ``mkstemp`` creates the inode as this process's user, and ``os.replace``
    keeps that inode, so without this a tag edit on a file another user owns
    would silently hand it to the writer. When that owner cannot be kept (a
    non-root writer may not give a file away) this raises, which aborts the
    rewrite. When only the group differs (a setgid directory, or macOS, which
    always takes the directory's group) and the writer is not in the
    original group, the write proceeds with the directory's group, as every
    rewrite did before ownership was kept: refusing would block ordinary tag
    edits on the writer's own files. Platform seam: Windows has no
    ``os.chown``, and its owner comes from the directory, so nothing is done.
    """
    if not hasattr(os, "chown"):
        return
    current = tmp.stat()
    if current.st_uid != original.st_uid:
        os.chown(tmp, original.st_uid, original.st_gid)
    elif current.st_gid != original.st_gid:
        try:
            os.chown(tmp, -1, original.st_gid)
        except PermissionError:
            pass  # the writer's own file; the group is the directory's, as before


# copyfile(3) flags, <copyfile.h>: COPYFILE_ACL | COPYFILE_XATTR.
_COPYFILE_ACL_AND_XATTR = (1 << 0) | (1 << 2)
_XATTR_UNSUPPORTED = {errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
#: Failures that mean "this process may not set that attribute here", not a
#: broken copy: a non-root writer can list ``security.*`` and ``system.*``
#: names it cannot set on a new inode.
_XATTR_NOT_PERMITTED = _XATTR_UNSUPPORTED | {errno.EPERM, errno.EACCES}
#: Only user-namespace attributes (Finder-style tags, app data) must survive;
#: the kernel and policy namespaces are copied best effort.
_REQUIRED_XATTR_PREFIX = "user."


def copy_extended_metadata(src: Path, dst: Path) -> None:
    """Carry ``src``'s extended attributes (and, on macOS, its ACL) onto ``dst``.

    The replace swaps in a brand-new inode, so without this a tag edit would
    silently drop Finder tags, quarantine flags and access-control entries:
    more than the writer promises to change. A failure raises, which aborts
    the rewrite and leaves the original untouched.

    Platform seam: macOS uses copyfile(3), which copies both; Linux copies
    each xattr (POSIX ACLs live in xattrs there); a filesystem without xattr
    support has none to lose. Windows has no xattr API in Python and its ACLs
    are inherited from the directory, so nothing is copied there. On Linux a
    ``user.*`` attribute that cannot be set aborts the write; ``security.*``,
    ``system.*`` and ``trusted.*`` ones the process may not set (SELinux
    labels, capabilities) are skipped, so they never block a tag write.
    """
    if sys.platform == "darwin":
        # A literal path the payload runtime-load guard can classify (allowlisted as
        # part of every macOS install); copyfile(3) is exported by libSystem.
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        if libc.copyfile(os.fsencode(src), os.fsencode(dst), None, _COPYFILE_ACL_AND_XATTR) < 0:
            code = ctypes.get_errno()
            if code in _XATTR_UNSUPPORTED:
                return
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
        try:
            os.setxattr(dst, name, os.getxattr(src, name))
        except OSError as exc:
            if exc.errno == errno.ENODATA:
                continue  # removed between list and get: nothing to keep
            if name.startswith(_REQUIRED_XATTR_PREFIX) or exc.errno not in _XATTR_NOT_PERMITTED:
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


__all__ = [
    "copy_range",
    "copy_rest",
    "keep_ownership",
    "replace_head",
    "replace_range",
    "rewrite_atomic",
]
