"""Where macOS keeps ``diskutil``, pinned: never looked up on PATH.

``diskutil`` ships at ``/usr/sbin/diskutil`` on every macOS. A PATH lookup
adds nothing on a Mac and takes the tool away from any process whose PATH
has no ``/usr/sbin``, which is what launchd hands a user agent that sets its
own PATH: the preview engine answered 503 ``diskutil_unavailable`` to every
USB volume request for that reason alone (Thu 1 Oct 2026).

Readers dereference ``macos_diskutil.DISKUTIL_PATH`` at call time, so a test
can point it at a file of its own.
"""
from __future__ import annotations

import os
from pathlib import Path

DISKUTIL_PATH: Path = Path("/usr/sbin/diskutil")


def resolve_diskutil(path: Path | None = None) -> str | None:
    """The absolute diskutil path when it is an executable file, else None.

    None is the caller's cue to fail loudly (``diskutil_unavailable``); it is
    never replaced by a PATH search.
    """
    candidate = DISKUTIL_PATH if path is None else path
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


__all__ = ["DISKUTIL_PATH", "resolve_diskutil"]
