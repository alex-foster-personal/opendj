"""Opening a plugin file for reading only when it is a regular file.

Every file the plugin store reads back (an installed payload or manifest, an
install receipt, a cached catalog or screenshot, the state file) can be
replaced on disk by a FIFO or a device. A plain ``open()`` of a FIFO blocks
until a writer appears, and a read of a device can block or never end, so one
swapped file could hang a request or stop the engine from booting before any
size cap or error handling ran.

:func:`open_regular_file` opens non-blocking and checks the open descriptor
itself (no check-then-open race), so such a file is refused with
:class:`NotRegularFileError`, an :class:`OSError`, and every caller's existing
handling of an unreadable file applies. Windows has no ``O_NONBLOCK`` and no
FIFOs in the filesystem; the descriptor check still runs there.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import IO


class NotRegularFileError(OSError):
    """The path is a FIFO, device, socket or folder, not a regular file."""


def open_regular_file(path: Path) -> IO[bytes]:
    """``path`` opened for binary reading; raises :class:`NotRegularFileError` unless it is a regular file."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
    handle = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        handle.close()
        raise NotRegularFileError(f"{path.name} is not a regular file")
    return handle
