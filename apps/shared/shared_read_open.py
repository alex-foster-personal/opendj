"""Open a file for reading without blocking a rename or delete of it (platform seam).

On POSIX an open descriptor never stops another process from renaming or
unlinking the file or its directory. On Windows the CRT's ``os.open`` asks
for read and write sharing only, so while the descriptor is open a rename of
the file, a rename of a directory holding it, or a delete fails with a
sharing violation. A stem being streamed to a deck would then make a bundle
swap or a backup cleanup fail for as long as playback reads it (Codex on
#6820). This module opens through ``CreateFileW`` with ``FILE_SHARE_DELETE``
on Windows so the reader behaves as it does on POSIX.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["open_read_shared"]


def open_read_shared(path: Path) -> int:
    """Return a read-only descriptor for ``path`` that never pins its name.

    A symlink is refused rather than followed: ``O_NOFOLLOW`` on POSIX, and on
    Windows the reparse point itself is opened, which a caller comparing the
    descriptor's stat to the path's ``lstat`` rejects.
    """
    if sys.platform == "win32":
        return _open_windows(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _GENERIC_READ = 0x80000000
    _FILE_SHARE_ALL = 0x1 | 0x2 | 0x4  # READ | WRITE | DELETE
    _OPEN_EXISTING = 3
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL

    def _open_windows(path: Path) -> int:
        handle = _CreateFileW(
            str(path),
            _GENERIC_READ,
            _FILE_SHARE_ALL,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle is None or handle == _INVALID_HANDLE_VALUE:
            code = ctypes.get_last_error()
            raise OSError(None, ctypes.FormatError(code), str(path), code)
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _CloseHandle(handle)
            raise
