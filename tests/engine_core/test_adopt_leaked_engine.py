"""Gate 3: a leaked engine is adoptable on the next start (issue #2160)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from apps.engine_core.lock import EngineLock, EngineLockError, inspect_lock, read_holder

_LEAKED_HOLDER = textwrap.dedent(
    """
    import signal, sys, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path

    from apps.engine_core.lock import EngineLock

    lock_path = Path(sys.argv[1])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/api/v1/health", "/performance"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    lock = EngineLock(lock_path, host="127.0.0.1", port=port)
    lock.acquire()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(port, flush=True)
    signal.pause()
    """
)


def _start_leaked_holder(lock_path: Path) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", _LEAKED_HOLDER, str(lock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    assert line.isdigit(), f"leaked holder did not report port: {line!r}"
    return proc


def _http_get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.mark.requirement("INSTALL-14")
def test_leaked_engine_is_adoptable_and_singleton_stays_enforced(tmp_path: Path) -> None:
    lock_path = tmp_path / ".engine.lock"
    leaked = _start_leaked_holder(lock_path)
    try:
        inspected = inspect_lock(lock_path)
        assert inspected != "free"
        holder = read_holder(lock_path)
        assert holder is not None
        assert holder.pid == leaked.pid
        assert holder.port is not None

        origin = f"http://127.0.0.1:{holder.port}"
        assert _http_get(f"{origin}/api/v1/health") == 200
        assert _http_get(f"{origin}/performance") == 200

        with pytest.raises(EngineLockError) as refusal:
            EngineLock(lock_path, boot_id="second").acquire()
        assert "alive and healthy" in str(refusal.value)
    finally:
        leaked.send_signal(signal.SIGTERM)
        leaked.wait(timeout=5)

    assert inspect_lock(lock_path) == "free"
    lock = EngineLock(lock_path, boot_id="after-leak")
    lock.acquire()
    lock.release()

