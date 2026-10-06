"""Platform seam: start a child process at background CPU priority.

POSIX lowers the child's niceness in ``preexec_fn``; Windows has neither
``preexec_fn`` nor ``os.nice``, so it asks for ``BELOW_NORMAL_PRIORITY_CLASS``
through ``creationflags`` instead. Spread the result into ``subprocess.run``.

macOS also needs ``background_argv``: niceness alone still lets a child (and
the ffmpeg it starts) take performance cores. ``taskpolicy -b`` puts it in the
background QoS band (efficiency cores, throttled I/O), and its descendants
inherit that (Tue 6 Oct 2026: the loudness drain's 4 ffmpeg workers starved
the API on silver at load 18-24).
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

#: macOS's background-QoS launcher. Part of the base system; never optional.
TASKPOLICY: str = "/usr/sbin/taskpolicy"


def lowered_priority(niceness: int) -> dict[str, Any]:
    """``subprocess`` keyword arguments that start the child below normal priority."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS")}  # noqa: B009
    return {"preexec_fn": lambda: os.nice(niceness)}


def background_argv(
    argv: list[str], *, platform: str | None = None, taskpolicy: str | None = None
) -> list[str]:
    """``argv`` started in macOS's background QoS band; unchanged elsewhere.

    Fails fast when ``taskpolicy`` is missing on darwin: a silent nice-only
    fallback would put the background work back on the performance cores.
    """
    if (platform or sys.platform) != "darwin":
        return list(argv)
    taskpolicy = taskpolicy or TASKPOLICY
    if not os.access(taskpolicy, os.X_OK):
        raise FileNotFoundError(
            f"{taskpolicy} is missing or not executable; background work refuses to start "
            "without macOS background QoS"
        )
    return [taskpolicy, "-b", *argv]


_BELOW_NORMAL_PRIORITY_CLASS = 0x4000


def lower_own_priority(niceness: int) -> None:
    """Drop the calling process to background CPU priority, on any platform."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)  # noqa: B009
        if not kernel32.SetPriorityClass(
            kernel32.GetCurrentProcess(), _BELOW_NORMAL_PRIORITY_CLASS
        ):
            raise OSError(ctypes.get_last_error(), "SetPriorityClass failed")
        return
    os.nice(niceness)
