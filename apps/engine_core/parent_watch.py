"""Exit the engine when the desktop shell parent pid disappears, or when the shell asks.

Two triggers, one outcome: the engine's own SIGTERM handler runs, so uvicorn's
graceful shutdown (bounded, see ``__main__``) happens instead of a hard kill.

- The watched parent pid is gone (``OPENDJ_PARENT_PID``, rebindable through
  ``.engine.parent``).
- The shell wrote ``.engine.stop`` in the data directory. This is the polite
  stop for Windows, where a headless engine has no window to receive a close
  and ``taskkill`` without ``/F`` fails; the shell waits its grace for the exit
  and only then forces the process tree. It works the same on every OS and has
  no network surface: the data directory is the user's own.

Windows specifics: liveness is ``apps.shared.pid_liveness.pid_is_running``
(``os.kill(pid, 0)`` is ``CTRL_C_EVENT`` there), and the self-signal is
``signal.raise_signal`` because ``os.kill(os.getpid(), SIGTERM)`` is
``TerminateProcess`` there and runs no handler.

[if] the parent is gone or .engine.stop appears and the SIGTERM handler does not run [then] the engine dies hard or never stops, [else stop]
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

from apps.shared.process_liveness import pid_is_alive

PARENT_ENV = "OPENDJ_PARENT_PID"
PARENT_FILE_NAME = ".engine.parent"
STOP_FILE_NAME = ".engine.stop"
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
    except (OSError, UnicodeDecodeError):
        # Non-UTF-8 bytes are just another malformed marker: fall back to the
        # env pid rather than end the watch thread and orphan the engine.
        return None


def _consume_stop_request(data_dir: Path) -> bool:
    """True when the shell asked for a stop; the request is removed so it is never replayed."""
    try:
        (data_dir / STOP_FILE_NAME).unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        # The request exists but cannot be removed: honor it anyway, and say why.
        print(f"[WARN] could not remove {STOP_FILE_NAME}: {exc}", file=sys.stderr)
    return True


def _set_pdeathsig_linux() -> None:
    if not sys.platform.startswith("linux"):
        return
    import ctypes

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    PR_SET_PDEATHSIG = 1
    libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


def _signal_self_term() -> None:
    if sys.platform == "win32":
        signal.raise_signal(signal.SIGTERM)
    else:
        os.kill(os.getpid(), signal.SIGTERM)


def _warn_parent_gone(pid: int) -> None:
    """Best-effort warning: stderr is usually a pipe the dead shell was reading.

    Python ignores SIGPIPE, so that write raises BrokenPipeError. Raised here it
    would end the watch thread before the SIGTERM and orphan the engine
    (INSTALL-42), so only the warning is dropped, never the stop.
    """
    try:
        print(f"[WARN] parent pid {pid} is gone; stopping this engine", file=sys.stderr, flush=True)
    except OSError:
        return


def _watch_loop(data_dir: Path, env_pid: int) -> None:
    while True:
        if _consume_stop_request(data_dir):
            print(f"[INFO] {STOP_FILE_NAME} found; stopping this engine", file=sys.stderr)
            _signal_self_term()
            return
        file_pid = _read_parent_file(data_dir)
        watched_pid = file_pid if file_pid is not None else env_pid
        if not pid_is_alive(watched_pid):
            _warn_parent_gone(watched_pid)
            _signal_self_term()
            return
        time.sleep(POLL_INTERVAL_S)


def start(data_dir: Path) -> None:
    """Start watching the shell parent when ``OPENDJ_PARENT_PID`` is set."""
    env_pid = _parse_positive_pid(os.environ.get(PARENT_ENV))
    if env_pid is None:
        return
    _set_pdeathsig_linux()
    # A request left by an earlier run must not stop this engine: the shell
    # writes one only after it has seen this engine start.
    (data_dir / STOP_FILE_NAME).unlink(missing_ok=True)
    thread = threading.Thread(
        target=_watch_loop,
        args=(data_dir, env_pid),
        name="parent-watch",
        daemon=True,
    )
    thread.start()


__all__ = ["PARENT_ENV", "PARENT_FILE_NAME", "STOP_FILE_NAME", "start"]
