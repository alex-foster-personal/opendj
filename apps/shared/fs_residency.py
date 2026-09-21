"""Filesystem residency: playable local bytes vs iCloud dataless stubs.

Path.exists() / Path.is_file() return True for APFS iCloud Drive placeholders
(Desktop & Documents sync). Those stubs report a logical st_size but allocate
no blocks (st_blocks == 0). Opening or FileResponse-serving them hangs or
yields fewer bytes than Content-Length.

Ops scripts already use this signal; this module is the shared library version.
Discovery must use stat only -- never open/read (that triggers bird materialise).
"""

from __future__ import annotations

import os
import stat as stat_module
import sys
from pathlib import Path


def is_dataless_stub(st: os.stat_result) -> bool:
    """True when ``st`` is a Darwin iCloud / sparse placeholder signature.

    Contract: non-zero logical size with zero allocated blocks. Zero-length
    regular files are not stubs (they can legitimately have st_blocks == 0).
    Off-Darwin always False -- this problem domain is macOS iCloud Drive.
    """
    if sys.platform != "darwin":
        return False
    return st.st_size > 0 and st.st_blocks == 0


def is_materialised(path: Path) -> bool:
    """True when ``path`` is a regular file with local bytes (not a dataless stub)."""
    try:
        st = path.stat()
    except OSError:
        return False
    if not stat_module.S_ISREG(st.st_mode):
        return False
    return not is_dataless_stub(st)


def exists_for_audio_open_probe(path: Path) -> bool:
    """True when a library path exists locally and an ``open()`` probe may run.

    Materialised regular files and FIFOs both qualify: a FIFO with no writer
    blocks in-kernel the same way a wedged macOS Media Library prompt does, and
    must reach the bounded-open probe instead of being treated as missing (#2749).
    """
    try:
        st = path.stat()
    except OSError:
        return False
    if stat_module.S_ISFIFO(st.st_mode):
        return True
    if not stat_module.S_ISREG(st.st_mode):
        return False
    return not is_dataless_stub(st)


def materialised_size(path: Path) -> int | None:
    """Byte size when materialised regular file; None when missing / stub / not a file."""
    try:
        st = path.stat()
    except OSError:
        return None
    if not stat_module.S_ISREG(st.st_mode):
        return None
    if is_dataless_stub(st):
        return None
    return st.st_size
