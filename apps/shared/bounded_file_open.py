"""Bounded, killable ``open()`` probes for library media paths (#2749).

A kernel-blocked ``open()`` cannot be cancelled from a Python thread: abandoning
the future leaves a wedged worker that starves later probes and can block engine
shutdown (#766). This module runs the syscall in a short-lived subprocess and
kills the process group on timeout so nothing leaks into the parent.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from apps.shared.process_groups import (
    WorkerCleanupError,
    group_has_live_member,
    signal_group,
    wait_group_gone,
)

_WINDOWS = sys.platform == "win32"

# Shared knob for preflight and deck-load probes.
AUDIO_ACCESS_TIMEOUT_S: float = 3.0
_PROBE_TERMINATE_GRACE_S: float = 2.0


@dataclass(frozen=True)
class BoundedOpenResult:
    outcome: Literal["ok", "timeout", "error"]
    elapsed_s: float
    errno: int | None = None
    message: str | None = None


class BoundedFileOpenError(RuntimeError):
    """The probe failed before a readable byte could be confirmed."""

    def __init__(
        self,
        *,
        timed_out: bool,
        path: Path,
        timeout_s: float,
        errno: int | None = None,
        message: str | None = None,
    ) -> None:
        self.timed_out = timed_out
        self.path = path
        self.timeout_s = timeout_s
        self.errno = errno
        self.message = message
        super().__init__(message or f"bounded open failed for {path}")


def _worker_command(path: Path) -> list[str]:
    return [sys.executable, "-m", "apps.shared.bounded_file_open_worker", str(path)]


def _process_kwargs() -> dict[str, Any]:
    if _WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate_probe_worker(proc: subprocess.Popen[bytes]) -> None:
    if _WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        proc.wait(timeout=_PROBE_TERMINATE_GRACE_S)
        return

    process_group_id = proc.pid
    try:
        if signal_group(process_group_id, signal.SIGTERM) is not None:
            proc.wait()
            return
        try:
            proc.wait(timeout=_PROBE_TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
        if group_has_live_member(process_group_id):
            signal_group(process_group_id, signal.SIGKILL)
        proc.wait()
        wait_group_gone(process_group_id, _PROBE_TERMINATE_GRACE_S)
    except WorkerCleanupError:
        raise
    except BaseException as exc:
        raise WorkerCleanupError(
            f"bounded open worker process group {process_group_id} cleanup is unverified"
        ) from exc


def _parse_worker_stderr(stderr: bytes) -> tuple[int | None, str]:
    text = stderr.decode("utf-8", errors="replace").strip()
    if not text:
        return None, "worker exited with an error"
    if ":" in text:
        prefix, rest = text.split(":", 1)
        try:
            return int(prefix), rest.strip() or text
        except ValueError:
            pass
    return None, text


def probe_readable_byte(path: Path, *, timeout_s: float) -> BoundedOpenResult:
    """Open ``path`` in a subprocess and read one byte within ``timeout_s``."""
    if timeout_s <= 0:
        raise ValueError(f"timeout_s must be > 0, got {timeout_s}")
    start = time.monotonic()
    proc = subprocess.Popen(
        _worker_command(path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **(_process_kwargs()),
    )
    try:
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _terminate_probe_worker(proc)
            proc.wait()
            return BoundedOpenResult(
                outcome="timeout",
                elapsed_s=time.monotonic() - start,
            )
        stderr = proc.stderr.read() if proc.stderr is not None else b""
        elapsed_s = time.monotonic() - start
        if proc.returncode == 0:
            return BoundedOpenResult(outcome="ok", elapsed_s=elapsed_s)
        errno_value, message = _parse_worker_stderr(stderr)
        return BoundedOpenResult(
            outcome="error",
            elapsed_s=elapsed_s,
            errno=errno_value,
            message=message,
        )
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()


__all__ = [
    "AUDIO_ACCESS_TIMEOUT_S",
    "BoundedFileOpenError",
    "BoundedOpenResult",
    "probe_readable_byte",
]
