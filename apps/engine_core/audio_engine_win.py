"""What starting ``odj-audio`` needs on Windows only (GSD plan 20-02).

``audio_engine.py`` starts the engine the same way on every OS: three pipes,
stdin held open for the engine's life. Two things a POSIX parent never has to
ask for are missing on Windows, and both are added here.

No console window. ``odj-audio`` is a console program. A console program
started by a process that has no console of its own is given a NEW console,
with a window. The Python engine inside the desktop app has none (the shell
starts it detached), so every engine start and every restart would open a
window beside the app. Measured on Windows 11, Thu 1 Oct 2026: a child of a
``DETACHED_PROCESS`` parent reported a visible console window with default
flags, and none with ``CREATE_NO_WINDOW``. The flag also gives the engine a
console group of its own, so a Ctrl+C typed into a developer's terminal stops
this process, which then stops the engine in order.

No life after this process. The engine leaves on stdin end-of-file, which
the kernel delivers when this process dies however it dies. That needs the
engine to be reading: one stuck inside a device call or a long decode stays,
holding the output device. A Job Object with ``KILL_ON_JOB_CLOSE`` is the
kernel's own rule for it: when the last handle to the job closes, every
process in the job is ended, and this process holds the only handle. Jobs
nest (Windows 8 and later), so this works inside the shell's own job. The
engine starts no children, so it is assigned after it exists; the gap before
the assignment is covered by end-of-file as before.

POSIX gets neither: ``popen_kwargs`` is empty and ``bind_to_this_process``
returns ``None`` there, leaving end-of-file as the one mechanism.

Requirements (mini-PRD, NAE-08 in ``.planning/REQUIREMENTS.md``):
  [if] a process with no console starts the engine and a console window
    opens [then] broken ⛔️
  [if] this process is killed outright and an engine that is not reading
    stdin is still running afterwards [then] broken ⛔️
  [if] the job cannot be made or the engine cannot be put in it, and the
    engine is left running unbound [then] broken ⛔️
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any


class LifetimeBinding:
    """The only handle to the engine's job. Closing it ends what is in the job."""

    def __init__(self, job: int) -> None:
        self._job: int | None = job

    def close(self) -> None:
        """Close the job. Idempotent; an engine still in it is ended."""
        job, self._job = self._job, None
        if sys.platform == "win32" and job is not None:
            _close(job)


def popen_kwargs() -> dict[str, Any]:
    """Extra ``Popen`` keywords for the engine: no console window on Windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def set_blocking(fd: int, blocking: bool) -> None:
    """``os.set_blocking`` for a pipe, on every OS.

    ``os.set_blocking`` does not take pipes on Windows before Python 3.12 (it
    raises ``AttributeError`` on 3.11), so Windows asks the kernel directly
    (``PIPE_NOWAIT``/``PIPE_WAIT``). Raises ``OSError`` when the mode cannot be
    set, and the caller decides what an unclosable pipe means.
    """
    if sys.platform == "win32":
        _set_pipe_blocking(fd, blocking)
    else:
        os.set_blocking(fd, blocking)


def bind_to_this_process(proc: subprocess.Popen[str]) -> LifetimeBinding | None:
    """Have the kernel end ``proc`` when this process ends (Windows).

    Returns the binding to close once ``proc`` has exited, or ``None`` where
    there is nothing to hold: on POSIX, and for an engine that had already
    exited (its exit is reported as any exit is). Raises ``OSError`` when a
    running engine could not be bound; the caller must not leave it running.
    """
    if sys.platform == "win32":
        try:
            return LifetimeBinding(_job_holding(proc.pid))
        except OSError:
            if proc.poll() is not None:
                return None
            raise
    return None


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100

    class _BasicLimits(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _IoCounters(ctypes.Structure):
        _fields_ = (
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        )

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    def _require(ok: object, what: str) -> None:
        """Raise the calling thread's last Windows error, named, unless ``ok``."""
        if not ok:
            error = ctypes.WinError(ctypes.get_last_error())
            error.strerror = f"{what}: {error.strerror}"
            raise error

    def _close(handle: int) -> None:
        _require(_kernel32.CloseHandle(handle), "CloseHandle")

    _PIPE_WAIT = 0x00000000
    _PIPE_NOWAIT = 0x00000001
    _kernel32.SetNamedPipeHandleState.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    _kernel32.SetNamedPipeHandleState.restype = wintypes.BOOL

    def _set_pipe_blocking(fd: int, blocking: bool) -> None:
        """``PIPE_WAIT`` or ``PIPE_NOWAIT`` on the pipe behind C file descriptor ``fd``."""
        import msvcrt

        mode = wintypes.DWORD(_PIPE_WAIT if blocking else _PIPE_NOWAIT)
        _require(
            _kernel32.SetNamedPipeHandleState(msvcrt.get_osfhandle(fd), ctypes.byref(mode), None, None),
            "SetNamedPipeHandleState",
        )

    def _job_holding(pid: int) -> int:
        """A kill-on-close job with process ``pid`` in it; the job's handle.

        The handle is not inheritable (no security attributes), so no child
        of this process can keep the job, and the engine in it, alive.
        """
        job: int | None = _kernel32.CreateJobObjectW(None, None)
        _require(job, "CreateJobObjectW")
        assert job is not None
        try:
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _require(
                _kernel32.SetInformationJobObject(
                    job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
                ),
                "SetInformationJobObject",
            )
            # The pid cannot have been reused: the caller's Popen still holds
            # the process open.
            process: int | None = _kernel32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, pid)
            _require(process, f"OpenProcess({pid})")
            assert process is not None
            try:
                _require(_kernel32.AssignProcessToJobObject(job, process), f"AssignProcessToJobObject({pid})")
            finally:
                _close(process)
        except BaseException:
            # Nothing was put in the job, so closing it ends nothing.
            _close(job)
            raise
        return job


__all__ = ["LifetimeBinding", "bind_to_this_process", "popen_kwargs", "set_blocking"]
