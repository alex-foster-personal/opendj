"""Platform seam: start a child process at background CPU priority.

POSIX lowers the child's niceness in ``preexec_fn``; Windows has neither
``preexec_fn`` nor ``os.nice``, so it asks for ``BELOW_NORMAL_PRIORITY_CLASS``
through ``creationflags`` instead. Spread the result into ``subprocess.run``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any


def lowered_priority(niceness: int) -> dict[str, Any]:
    """``subprocess`` keyword arguments that start the child below normal priority."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS")}  # noqa: B009
    return {"preexec_fn": lambda: os.nice(niceness)}


_BELOW_NORMAL_PRIORITY_CLASS = 0x4000


def lower_own_priority(niceness: int) -> None:
    """Drop the calling process to background CPU priority, on any platform."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009
        if not kernel32.SetPriorityClass(
            kernel32.GetCurrentProcess(), _BELOW_NORMAL_PRIORITY_CLASS
        ):
            raise OSError(ctypes.get_last_error(), "SetPriorityClass failed")
        return
    os.nice(niceness)
