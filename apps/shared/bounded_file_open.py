"""Bounded, killable ``open()`` probes for library media paths (#2749).

A kernel-blocked ``open()`` cannot be cancelled from a Python thread: abandoning
the future leaves a wedged worker that starves later probes and can block engine
shutdown (#766). This module runs the syscall in a short-lived subprocess and
stops the worker's whole process tree on timeout (``apps.shared.process_tree``)
so nothing leaks into the parent, on every OS.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apps.shared.process_tree import ProcessTreeError, TreeRoot, spawn_tree, stop_tree

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


# The probe child is an isolated interpreter running this inline program: no
# site/.pth processing (-S), no user site or cwd on sys.path (-I), and no
# import of the apps package, so its start-up is the bare interpreter (about
# 17 ms vs 48 ms for ``-m apps.shared.bounded_file_open_worker`` on Linux).
# It does exactly what bounded_file_open_worker.main does and writes errors in
# the same ``<errno>:<message>`` form (tests/shared/test_bounded_file_open.py
# pins the two to the same exit code and errno). The leading comment names the
# worker so a process listing still identifies the probe child. Every flag
# here is portable: -I, -S and -c behave the same on macOS, Linux and Windows.
_INLINE_WORKER_SOURCE = """\
# bounded_file_open_worker (inline)
import sys
try:
    with open(sys.argv[1], "rb") as fh:
        fh.read(1)
except OSError as exc:
    sys.stderr.write(f"{exc.errno}:{exc}")
    sys.exit(1)
"""


def _worker_command(path: Path) -> list[str]:
    return [sys.executable, "-I", "-S", "-c", _INLINE_WORKER_SOURCE, str(path)]


def _spawn_probe_worker(path: Path) -> tuple[subprocess.Popen[bytes], TreeRoot]:
    return spawn_tree(
        _worker_command(path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _stop_probe_worker(proc: subprocess.Popen[bytes], root: TreeRoot) -> None:
    """Stop the worker's whole tree, then reap the handle; raises if any of it survives."""
    stop_tree(root, grace_s=_PROBE_TERMINATE_GRACE_S)
    try:
        proc.wait(timeout=_PROBE_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired as exc:
        raise ProcessTreeError(f"probe worker {proc.pid} was stopped but its handle did not exit") from exc


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
    proc, root = _spawn_probe_worker(path)
    try:
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _stop_probe_worker(proc, root)
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
