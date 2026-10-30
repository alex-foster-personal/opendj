"""Portable probe for whether a process ID still exists."""

from __future__ import annotations

import os

__all__ = ["pid_is_alive"]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87


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
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        if not kernel32.CloseHandle(handle):
            error = ctypes.get_last_error()
            raise getattr(ctypes, "WinError")(error)  # noqa: B009
        return True
    error = ctypes.get_last_error()
    if error == _ERROR_ACCESS_DENIED:
        return True
    if error == _ERROR_INVALID_PARAMETER:
        return False
    raise getattr(ctypes, "WinError")(error)  # noqa: B009


def pid_is_alive(pid: int) -> bool:
    """Whether ``pid`` still exists."""
    if os.name == "nt":
        return _win_pid_is_alive(pid)
    return _posix_pid_is_alive(pid)
