"""Exit the engine when the desktop shell parent pid disappears."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

PARENT_ENV = "OPENDJ_PARENT_PID"
PARENT_FILE_NAME = ".engine.parent"
POLL_INTERVAL_S = 0.25


def _parse_positive_pid(raw: str | None) -> int | None:
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        pid = int(stripped)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _read_parent_file(data_dir: Path) -> int | None:
    path = data_dir / PARENT_FILE_NAME
    if not path.is_file():
        return None
    try:
        return _parse_positive_pid(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _set_pdeathsig_linux() -> None:
    if not sys.platform.startswith("linux"):
        return
    import ctypes

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    PR_SET_PDEATHSIG = 1
    libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


def _watch_loop(data_dir: Path, env_pid: int) -> None:
    while True:
        file_pid = _read_parent_file(data_dir)
        watched_pid = file_pid if file_pid is not None else env_pid
        if not _is_alive(watched_pid):
            print(
                f"[WARN] parent pid {watched_pid} is gone; stopping this engine",
                file=sys.stderr,
            )
            os.kill(os.getpid(), signal.SIGTERM)
            return
        time.sleep(POLL_INTERVAL_S)


def start(data_dir: Path) -> None:
    """Start watching the shell parent when ``OPENDJ_PARENT_PID`` is set."""
    env_pid = _parse_positive_pid(os.environ.get(PARENT_ENV))
    if env_pid is None:
        return
    _set_pdeathsig_linux()
    thread = threading.Thread(
        target=_watch_loop,
        args=(data_dir, env_pid),
        name="parent-watch",
        daemon=True,
    )
    thread.start()


__all__ = ["PARENT_ENV", "PARENT_FILE_NAME", "start"]
