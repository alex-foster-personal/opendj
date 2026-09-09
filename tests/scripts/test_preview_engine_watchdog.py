"""Acceptance tests for the preview engine's real HTTP liveness watchdog."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


@contextmanager
def _health_server(status: int = 200, body: bytes = b'{"ok":true}') -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api/v1/health"
    finally:
        server.shutdown()
        thread.join()


def _run(tmp_path: Path, health_url: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.preview_engine_watchdog",
            "--health-url",
            health_url,
            "--state-file",
            str(tmp_path / "watchdog-state.json"),
            "--restart-command",
            f"{sys.executable} -c 'raise SystemExit(0)'",
            *extra,
        ],
        check=False,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
    )


def test_healthy_http_response_clears_prior_failures(tmp_path: Path) -> None:
    """If health returns HTTP 200 then a stale failed count becomes zero, or broken."""
    state = tmp_path / "watchdog-state.json"
    state.write_text('{"consecutive_failures": 3}', encoding="utf-8")
    with _health_server() as health_url:
        result = _run(tmp_path, health_url)

    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text(encoding="utf-8")) == {"consecutive_failures": 0}
    assert '"event": "healthy"' in result.stdout


def test_first_timeout_records_failure_without_restarting(tmp_path: Path) -> None:
    """If one real HTTP probe cannot connect then it records failure, not restart, or broken."""
    result = _run(
        tmp_path,
        "http://127.0.0.1:1/api/v1/health",
        "--failure-threshold",
        "2",
        "--timeout-s",
        "0.1",
    )

    assert result.returncode == 1
    assert json.loads((tmp_path / "watchdog-state.json").read_text()) == {"consecutive_failures": 1}
    assert '"event": "unhealthy"' in result.stdout


def test_threshold_executes_restart_once_and_starts_cooldown(tmp_path: Path) -> None:
    """If the second real failed probe crosses the threshold then restart runs once, or broken."""
    marker = tmp_path / "restart-marker"
    command = f"{sys.executable} -c \"from pathlib import Path; Path(r'{marker}').touch()\""
    args = (
        "--failure-threshold",
        "2",
        "--timeout-s",
        "0.1",
        "--restart-command",
        command,
        "--restart-cooldown-s",
        "3600",
    )
    first = _run(tmp_path, "http://127.0.0.1:1/api/v1/health", *args)
    second = _run(tmp_path, "http://127.0.0.1:1/api/v1/health", *args)
    third = _run(tmp_path, "http://127.0.0.1:1/api/v1/health", *args)

    assert first.returncode == 1
    assert second.returncode == 2
    assert third.returncode == 1
    assert marker.exists()
    assert second.stdout.count('"event": "restart"') == 1
    assert '"event": "restart_suppressed"' in third.stdout
