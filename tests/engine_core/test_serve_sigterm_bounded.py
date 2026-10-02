"""A SIGTERM'd engine exits even while a request is still open (INSTALL-30).

[if] a request is still open when the engine gets SIGTERM [then] the engine stays alive after closing its listener, [else stop].

Seen on the Air, Fri 2 Oct 2026: ``apps.engine_core serve`` closed its
listener on SIGTERM and then sat in the event loop for 12+ minutes, holding
the engine lock, until ``kill -9``. uvicorn's graceful shutdown waits for
every open connection and task with no bound by default, and a page's event
stream or a handler blocked in a thread never finishes. These cases boot the
real ``serve`` entry point with one extra probe route and send it a real
SIGTERM.

Regression lines:
  - if an open event stream keeps a SIGTERM'd engine alive then quit hangs
    and the next boot finds the lock held -> broken
  - if a handler blocked in a worker thread keeps it alive then the same ->
    broken
  - if the probe cannot tell a bounded shutdown from an unbounded one then
    the two cases above prove nothing (the control) -> broken
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
    pytest.mark.requirement("INSTALL-30"),
    pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX SIGTERM delivery to the engine process"
    ),
]

REPO = Path(__file__).resolve().parents[2]
#: The lifespan shutdown after the graceful window, plus slack for a loaded runner.
EXIT_WITHIN_S: float = GRACEFUL_SHUTDOWN_S + 12.0

# Boots the real `serve` with two probe routes: a stream that never ends and
# a sync handler that blocks its worker thread.
_BOOT = """
import asyncio, sys, time
import apps.engine_core.__main__ as main_mod
import apps.engine_core.app as app_mod
from fastapi.responses import StreamingResponse
if sys.argv[1] == "unbounded":
    main_mod.GRACEFUL_SHUTDOWN_S = None
real_create_app = app_mod.create_app
def create_app(*args, **kwargs):
    app = real_create_app(*args, **kwargs)
    @app.get("/probe/blocking")
    def blocking():
        time.sleep(600)
        return {}
    @app.get("/probe/stream")
    async def stream():
        async def chunks():
            while True:
                yield b"data: x\\n\\n"
                await asyncio.sleep(1)
        return StreamingResponse(chunks(), media_type="text/event-stream")
    return app
app_mod.create_app = create_app
sys.exit(main_mod.main(sys.argv[2:]))
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _boot(tmp_path: Path, mode: str) -> tuple[subprocess.Popen[bytes], int]:
    port = _free_port()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-c", _BOOT, mode, "serve", "--data-dir", str(data_dir), "--port", str(port)],
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
    pytest.fail("the engine never answered /api/v1/health")


def _open_request(port: int, path: str) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    time.sleep(1.0)
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


@pytest.mark.parametrize("path", ["/probe/stream", "/probe/blocking"])
def test_sigterm_exits_with_a_request_still_open(tmp_path: Path, path: str) -> None:
    proc, port = _boot(tmp_path, "bounded")
    sock = _open_request(port, path)
    try:
        took = _seconds_to_exit(proc, EXIT_WITHIN_S)
        assert took is not None, f"engine still running {EXIT_WITHIN_S}s after SIGTERM with {path} open"
    finally:
        _stop(proc, sock)


def test_control_an_unbounded_shutdown_is_still_running(tmp_path: Path) -> None:
    proc, port = _boot(tmp_path, "unbounded")
    sock = _open_request(port, "/probe/stream")
    try:
        took = _seconds_to_exit(proc, GRACEFUL_SHUTDOWN_S + 5.0)
        assert took is None, f"control: the unbounded engine exited after {took:.1f}s, so the probe cannot see the hang"
    finally:
        _stop(proc, sock)
