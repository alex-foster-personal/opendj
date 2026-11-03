"""Hermetic tests for live-engine boot identity helpers (issue #7582).

The live module is slow and LANE_IGNORES'd; these run on the sharded lane.

[if] boot helpers accept an impostor [then] fail closed, [else stop].
"""
from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.health_lights.test_ahead_analysis_live_engine import (
    _answers_health,
    _logged_boot_id,
    _lost_port_race,
    _own_job,
    _SpawnedEngine,
)

pytestmark = pytest.mark.requirement("NATIVE-25")


def test_logged_boot_id_parses_the_ok_line(tmp_path: Path) -> None:
    """[if] the engine prints boot_id on the OK line [then] it is parsed, [else stop]."""
    log_path = tmp_path / "engine.log"
    log_path.write_text(
        "[OK] opendj engine 1.0.0 boot_id=abc-123 contract_rev=x data_dir=/d http://127.0.0.1:1\n",
        encoding="utf-8",
    )
    assert _logged_boot_id(log_path) == "abc-123"
    log_path.write_text("", encoding="utf-8")
    assert _logged_boot_id(log_path) is None


def test_health_from_another_process_on_the_port_is_not_this_engine(tmp_path: Path) -> None:
    """[if] health boot_id does not match the log [then] not this engine, [else stop]."""
    class _Impostor(http.server.BaseHTTPRequestHandler):
        boot_id = "someone-else"
        raw: bytes | None = None

        def do_GET(self) -> None:
            body = (
                self.raw
                if self.raw is not None
                else json.dumps({"status": "ok", "boot_id": self.boot_id}).encode()
            )
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Impostor)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        log_path = tmp_path / "engine.log"
        log_path.write_text(
            "[OK] opendj engine 1.0.0 boot_id=mine contract_rev=x data_dir=/d http://127.0.0.1:1\n",
            encoding="utf-8",
        )
        port = int(server.server_address[1])
        spawned = _SpawnedEngine(
            origin=f"http://127.0.0.1:{port}",
            proc=sleeper,
            log_path=log_path,
            port=port,
        )
        assert _answers_health(spawned) is False
        _Impostor.boot_id = "mine"
        assert _answers_health(spawned) is True
        _Impostor.boot_id = "mine"
        _Impostor.raw = b"<html>not json</html>"
        assert _answers_health(spawned) is False
        _Impostor.raw = None
        log_path.write_text("", encoding="utf-8")
        assert _answers_health(spawned) is False

        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        dead_spawned = _SpawnedEngine(
            origin=f"http://127.0.0.1:{port}",
            proc=dead,
            log_path=log_path,
            port=port,
        )
        log_path.write_text(
            "[OK] opendj engine 1.0.0 boot_id=mine contract_rev=x data_dir=/d http://127.0.0.1:1\n",
            encoding="utf-8",
        )
        with pytest.raises(AssertionError, match="engine exited"):
            _answers_health(dead_spawned)
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=10)
        server.shutdown()


def test_only_a_bind_collision_counts_as_a_lost_port_race(tmp_path: Path) -> None:
    """[if] bind collision is on one line with the endpoint [then] lost race, [else stop]."""
    port = 43210
    log_path = tmp_path / "engine.log"
    log_path.write_text(f"OSError: Address already in use: ('127.0.0.1', {port})\n", encoding="utf-8")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    spawned = _SpawnedEngine(
        origin=f"http://127.0.0.1:{port}",
        proc=dead,
        log_path=log_path,
        port=port,
    )
    assert _lost_port_race(spawned)

    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        live_spawned = _SpawnedEngine(
            origin=f"http://127.0.0.1:{port}",
            proc=live,
            log_path=log_path,
            port=port,
        )
        assert not _lost_port_race(live_spawned)
    finally:
        live.terminate()
        live.wait(timeout=10)

    log_path.write_text("ValueError: boom\n", encoding="utf-8")
    assert not _lost_port_race(spawned)

    log_path.write_text(
        "Address already in use\n"
        f"bind failed on ('127.0.0.1', {port})\n",
        encoding="utf-8",
    )
    assert not _lost_port_race(spawned)


def test_a_job_404_names_the_responding_server(tmp_path: Path) -> None:
    """[if] GET jobs returns 404 [then] fail with unknown server, [else stop]."""
    job_id = "job-deadbeef"

    class _Impostor(http.server.BaseHTTPRequestHandler):
        job_status: int = 404
        job_body: bytes | None = None

        def do_GET(self) -> None:
            if self.path.startswith("/api/v1/jobs/"):
                self.send_response(self.job_status)
                self.send_header("content-type", "application/json")
                self.end_headers()
                body = self.job_body or b'{"detail":"not found"}'
                self.wfile.write(body)
                return
            if self.path == "/api/v1/health":
                body = json.dumps({"status": "ok", "boot_id": "impostor"}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Impostor)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        port = int(server.server_address[1])
        log_path = tmp_path / "engine.log"
        log_path.write_text("", encoding="utf-8")
        spawned = _SpawnedEngine(
            origin=f"http://127.0.0.1:{port}",
            proc=sleeper,
            log_path=log_path,
            port=port,
        )
        base = f"http://127.0.0.1:{port}/api/v1"
        with pytest.raises(AssertionError, match="unknown to the responding server") as exc_info:
            _own_job(spawned, base, job_id)
        message = str(exc_info.value)
        assert job_id in message
        assert "engine.poll()" in message

        _Impostor.job_status = 200
        _Impostor.job_body = json.dumps({"id": job_id, "status": "running"}).encode()
        row = _own_job(spawned, base, job_id)
        assert row["id"] == job_id
        assert row["status"] == "running"
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=10)
        server.shutdown()
