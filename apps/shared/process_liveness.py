"""Portable probe for whether a process ID still exists."""

from __future__ import annotations

import os

__all__ = ["pid_is_alive"]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5


def _posix_pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _win_pid_is_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == _ERROR_ACCESS_DENIED


def pid_is_alive(pid: int) -> bool:
    """Whether ``pid`` still exists."""
    if os.name == "nt":
        return _win_pid_is_alive(pid)
    return _posix_pid_is_alive(pid)
