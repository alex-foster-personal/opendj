"""Exclusive, non-blocking, process-held file lock: the one platform seam for it.

``try_lock(fd)`` takes the lock or returns False at once when another open file
description holds it; ``unlock(fd)`` drops it. The OS releases the lock when
the holding descriptor closes or its process dies, so a crashed holder never
leaves a stale lock behind. POSIX uses ``fcntl.flock``; Windows uses
``msvcrt.locking`` on one byte.

Windows locks are mandatory byte-range locks: a byte another handle holds
cannot even be READ through a second handle. Callers keep readable content in
the locked file (the engine lock's holder JSON), so the Windows lock byte sits
at ``NT_LOCK_OFFSET``, far past any content; Windows allows locking past end of
file, so the file does not grow. Both arms leave the file position unchanged.

A caller whose Windows lock byte is already part of a deployed protocol passes
its own ``nt_offset``: CloudSync's ``.sync.lock`` has always locked byte 0 and
holds no content, so it keeps byte 0 (``SYNC_NT_LOCK_OFFSET``), or a round
started by pre-upgrade code and one started after the upgrade would lock
different bytes and both run (PR #6399 review). POSIX ignores ``nt_offset``.
"""

from __future__ import annotations

import errno
import os
import sys

NT_LOCK_OFFSET: int = 1 << 30
#: CloudSync ``.sync.lock``: byte 0, the Windows byte every released version locks.
SYNC_NT_LOCK_OFFSET: int = 0

if sys.platform == "win32":
    import msvcrt

    def _lock_byte(fd: int, mode: int, nt_offset: int) -> None:
        here = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, nt_offset, os.SEEK_SET)
        try:
            msvcrt.locking(fd, mode, 1)
        finally:
            os.lseek(fd, here, os.SEEK_SET)

    def try_lock(fd: int, *, nt_offset: int = NT_LOCK_OFFSET) -> bool:
        """Take the exclusive lock without blocking. False means another holder."""
        try:
            _lock_byte(fd, msvcrt.LK_NBLCK, nt_offset)
        except OSError as exc:
            if exc.errno != errno.EACCES:
                raise
            return False
        return True

    def unlock(fd: int, *, nt_offset: int = NT_LOCK_OFFSET) -> None:
        _lock_byte(fd, msvcrt.LK_UNLCK, nt_offset)

    def lock_exclusive(fd: int, *, nt_offset: int = NT_LOCK_OFFSET) -> None:
        """Block until the exclusive lock is acquired."""
        _lock_byte(fd, msvcrt.LK_LOCK, nt_offset)

elif os.name == "posix":
    import fcntl

    def try_lock(fd: int, *, nt_offset: int = NT_LOCK_OFFSET) -> bool:
        """Take the exclusive lock without blocking. False means another holder."""
        del nt_offset  # Windows-only: flock locks the whole file
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            return False
        return True

    def unlock(fd: int, *, nt_offset: int = NT_LOCK_OFFSET) -> None:
        del nt_offset  # Windows-only: flock locks the whole file
        fcntl.flock(fd, fcntl.LOCK_UN)

    def lock_exclusive(fd: int, *, nt_offset: int = NT_LOCK_OFFSET) -> None:
        """Block until the exclusive lock is acquired."""
        del nt_offset  # Windows-only: flock locks the whole file
        fcntl.flock(fd, fcntl.LOCK_EX)

else:
    raise ImportError(f"file_lock has no exclusive file lock for {sys.platform=}")


__all__ = ["NT_LOCK_OFFSET", "SYNC_NT_LOCK_OFFSET", "lock_exclusive", "try_lock", "unlock"]
