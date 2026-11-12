"""Shared helpers for the test_electron_dev test modules (split from test_electron_dev.py)."""

from __future__ import annotations

import http.server
import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts.electron_dev import (
    plan_launch,
)


def _plan(
    tmp_path: Path,
    *,
    frontend_port: int = 9400,
    platform: str = "darwin",
    environ: dict[str, str] | None = None,
    is_root: bool = False,
    xvfb_run: str | None = None,
):
    return plan_launch(
        electron_binary="/opt/electron",
        backend_port=8400,
        frontend_port=frontend_port,
        environ={"PATH": "/usr/bin"} if environ is None else environ,
        platform=platform,
        is_root=is_root,
        xvfb_run=xvfb_run,
        tmp_root=tmp_path,
    )


_RUN_BODY = (
    "import pathlib, sys, time\n"
    "from scripts.electron_dev import run\n"
    "def body():\n"
    "    try:\n"
    "        if sys.argv[2] == 'ctrl-c':\n"
    "            raise KeyboardInterrupt\n"
    "        print('up', flush=True)\n"
    "        time.sleep(60)\n"
    "        return 0\n"
    "    except KeyboardInterrupt:\n"
    "        return 0\n"
    "    finally:\n"
    "        pathlib.Path(sys.argv[1]).write_text('cleaned')\n"
    "raise SystemExit(run(body))\n"
)


def _group_with_a_reaped_leader(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A real group whose leader has exited and been reaped, with two live members left in it."""
    import subprocess
    import time

    import psutil

    ready = tmp_path / "member.pid"
    leader_src = (
        "import pathlib, subprocess, sys\n"
        "kids = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']) for _ in range(2)]\n"
        f"pathlib.Path({str(ready)!r}).write_text(' '.join(str(k.pid) for k in kids))\n"
    )
    leader = subprocess.Popen([sys.executable, "-c", leader_src], start_new_session=True)
    deadline = time.monotonic() + 20
    while not ready.exists() or not ready.read_text():
        assert time.monotonic() < deadline, "the leader never started its child"
        time.sleep(0.05)
    members = [psutil.Process(int(pid)) for pid in ready.read_text().split()]
    leader.wait(20)  # reaped: its pid number is now free
    assert all(os.getpgid(member.pid) == leader.pid for member in members)
    return leader, members


#: A stand-in window that is a real process: it answers /json/version with its
#: pid on the port in argv[1] and exits 0 once the file in argv[2] exists.
_WINDOW_SRC = """
import http.server, json, os, pathlib, sys, threading, time
port, close = int(sys.argv[1]), pathlib.Path(sys.argv[2])
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"pid": os.getpid()}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_):
        pass
server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
server.allow_reuse_address = True
threading.Thread(target=server.serve_forever, daemon=True).start()
while not close.exists():
    time.sleep(0.05)
"""


def _until(condition: object, what: str, timeout_s: float = 30.0) -> None:
    import time

    deadline = time.monotonic() + timeout_s
    while not condition():  # type: ignore[operator]
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    """Answers `/<code>` with that status; `/moved` redirects to `/200`."""

    def do_GET(self) -> None:
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/200")
        else:
            self.send_response(int(self.path.lstrip("/")))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def status_origin() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
