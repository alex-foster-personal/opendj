"""A SIGTERM'd engine exits even while a request is still open (INSTALL-33).

[if] a request is still open when the engine gets SIGTERM [then] the engine stays alive after closing its listener, [else stop].

Seen on the Air, Fri 2 Oct 2026: ``apps.engine_core serve`` closed its
listener on SIGTERM and then sat in the event loop for 12+ minutes, holding
the engine lock, until ``kill -9``. uvicorn's graceful shutdown waits for
every open connection and task with no bound by default, and a page's event
stream or a handler blocked in a thread never finishes. These cases boot the
real, unmodified ``serve`` entry point, leave a real request open against a
production route (a POST whose body never finishes arriving, so the handler
is still waiting on it) and send it a real SIGTERM.

Regression lines:
  - if a request whose body never finishes arriving keeps a SIGTERM'd engine
    alive then quit hangs and the next boot finds the lock held -> broken
  - if the engine exits before its graceful window is up then the open
    request did not hold the shutdown, and the case above proves nothing
    (the control) -> broken
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from apps.engine_core.__main__ import GRACEFUL_SHUTDOWN_S

pytestmark = [
    pytest.mark.requirement("INSTALL-33"),
    pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX SIGTERM delivery to the engine process"
    ),
]

REPO = Path(__file__).resolve().parents[2]
#: The lifespan shutdown after the graceful window, plus slack for a loaded runner.
EXIT_WITHIN_S: float = GRACEFUL_SHUTDOWN_S + 12.0
#: A production route whose handler reads a JSON body.
OPEN_REQUEST_PATH: str = "/api/v1/feedback/comments"

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _boot(tmp_path: Path) -> tuple[subprocess.Popen[bytes], int]:
    port = _free_port()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-m", "apps.engine_core", "serve", "--data-dir", str(data_dir), "--port", str(port)],
        cwd=REPO,
        env={**os.environ, "MDT_LIBRARY_MODE": "local"},
        stdout=subprocess.DEVNULL,
        stderr=(tmp_path / "engine.log").open("w"),
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        assert proc.poll() is None, (tmp_path / "engine.log").read_text()[-2000:]
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=1)
        except OSError:
            time.sleep(0.2)
        else:
            return proc, port
    proc.kill()
    proc.wait()
    raise AssertionError("the engine never answered /api/v1/health")


def _open_request(port: int) -> socket.socket:
    """POST a body that never finishes arriving, so the request stays open."""
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(
        (
            f"POST {OPEN_REQUEST_PATH} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n"
            '{"body":'
        ).encode()
    )
    time.sleep(1.0)
    sock.setblocking(False)
    with pytest.raises(BlockingIOError):
        sock.recv(1)  # no response yet: the request is still open
    return sock


def _seconds_to_exit(proc: subprocess.Popen[bytes], within_s: float) -> float | None:
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=within_s)
    except subprocess.TimeoutExpired:
        return None
    return time.monotonic() - started


def _stop(proc: subprocess.Popen[bytes], sock: socket.socket) -> None:
    sock.close()
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def test_sigterm_exits_with_a_request_still_open(tmp_path: Path) -> None:
    proc, port = _boot(tmp_path)
    sock = _open_request(port)
    try:
        took = _seconds_to_exit(proc, EXIT_WITHIN_S)
        assert took is not None, f"engine still running {EXIT_WITHIN_S}s after SIGTERM with a request open"
        # The control: the open request held the shutdown until the bound,
        # so it was the bound that ended it and not an early close.
        assert took >= GRACEFUL_SHUTDOWN_S - 0.5, (
            f"engine exited {took:.1f}s after SIGTERM, inside its {GRACEFUL_SHUTDOWN_S}s "
            "graceful window, so the open request never held the shutdown"
        )
    finally:
        _stop(proc, sock)
