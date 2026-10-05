"""Serve a stick file through the descriptor that was checked, not its path.

``stick_library`` proves a pdb path resolves inside the mount, but a writable
stick that holds symlinks could swap a directory or the leaf for a link after
that check, and ``FileResponse`` reopens the pathname when it streams. So the
route opens the validated leaf once (no symlink followed at the leaf), proves
that open file is the validated path, and streams that descriptor through its
``/proc/self/fd`` (Linux) or ``/dev/fd`` (macOS) name, which reopens the SAME
file whatever the stick's paths now point at (Codex on #4974, 7ee2306e4).

* [if] a stick path is swapped for a link between the check and the open [then]
  the route refuses ``USB_PATH_OUTSIDE_VOLUME``, [else stop].

Platform seam: Windows has no ``O_NOFOLLOW`` and no fd paths, and the FAT and
exFAT file systems sticks use there hold no symlinks, so the checked path is
served as it is.
"""
from __future__ import annotations

import contextlib
import errno
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from apps.shared.fd_anchored_walk import FD_ANCHORED_WALK_SUPPORTED, path_from_fd
from apps.sync.usb.stick_model import StickError

_IS_DARWIN: bool = sys.platform == "darwin"
_LEAF_FLAGS: int = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


@dataclass(frozen=True)
class PinnedStickFile:
    """``serve_path`` reopens the checked file; ``close`` releases it."""

    serve_path: str
    fd: int | None

    def close(self) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):  # already closed: nothing left to release
                os.close(self.fd)


def _same_path(real: Path, checked: Path) -> bool:
    if real == checked:
        return True
    # macOS volumes are usually case-insensitive and F_GETPATH reports the
    # on-disk case, which a pdb path need not match.
    return _IS_DARWIN and str(real).casefold() == str(checked).casefold()


def pin_stick_file(volume_uuid: str, checked: Path) -> PinnedStickFile:
    """Open ``checked`` (already proven contained) and pin what was opened."""
    if not FD_ANCHORED_WALK_SUPPORTED:
        return PinnedStickFile(serve_path=str(checked), fd=None)
    try:
        fd = os.open(checked, _LEAF_FLAGS)
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # the leaf itself became a link
            raise _swapped(volume_uuid, checked) from exc
        raise
    try:
        if not _same_path(path_from_fd(fd), checked):
            raise _swapped(volume_uuid, checked)
    except BaseException:
        os.close(fd)
        raise
    fd_dir = "/dev/fd" if _IS_DARWIN else "/proc/self/fd"
    return PinnedStickFile(serve_path=f"{fd_dir}/{fd}", fd=fd)


def _swapped(volume_uuid: str, checked: Path) -> StickError:
    return StickError(
        "USB_PATH_OUTSIDE_VOLUME",
        f"{str(checked)!r} changed into a link after it was checked (escapes_mount)",
        volume_uuid=volume_uuid,
        reason="escapes_mount",
    )


__all__ = ["PinnedStickFile", "pin_stick_file"]
