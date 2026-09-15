"""CLI integration tests for engine lock identity verification (issue #3042)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_NO_ENGINE
from apps.opendj_cli.__main__ import main
from tests.opendj_cli.conftest import Engine


def _argv(lock_path: Path, *tokens: str) -> list[str]:
    return ["--lock", str(lock_path), *tokens]


def _lock_payload(engine: Engine, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "pid": 4242,
        "role": "opendj-engine",
        "host": "127.0.0.1",
        "port": engine.port,
        "boot_id": "test-boot",
    }
    payload.update(overrides)
    return payload


def test_cli_boot_id_mismatch_exits_2(
    engine: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock_path = tmp_path / "bad.lock"
    lock_path.write_text(
        json.dumps(_lock_payload(engine, boot_id="wrong-boot")),
        encoding="utf-8",
    )

    assert main(["--json", *_argv(lock_path, "state")]) == EXIT_NO_ENGINE

    body = json.loads(capsys.readouterr().out)
    assert body["error"]["code"] == "engine_identity_mismatch"
    message = body["error"]["message"]
    assert "wrong-boot" in message
    assert "test-boot" in message
    assert str(lock_path) in message


def test_cli_wrong_role_exits_2(
    engine: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock_path = tmp_path / "bad.lock"
    lock_path.write_text(
        json.dumps(_lock_payload(engine, role="not-opendj-engine")),
        encoding="utf-8",
    )

    assert main(["--json", *_argv(lock_path, "state")]) == EXIT_NO_ENGINE

    body = json.loads(capsys.readouterr().out)
    assert body["error"]["code"] == "engine_identity_mismatch"
    assert "not-opendj-engine" in body["error"]["message"]


def test_cli_non_engine_port_exits_2_not_order_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    server = HTTPServer(("127.0.0.1", 0), _Html404Handler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    lock_path = tmp_path / "foreign.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 4242,
                "role": "opendj-engine",
                "host": "127.0.0.1",
                "port": port,
                "boot_id": "lock-boot",
            }
        ),
        encoding="utf-8",
    )

    try:
        assert main(["--json", *_argv(lock_path, "headphone_outputs_refresh")]) == (
            EXIT_NO_ENGINE
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert body["error"]["code"] in {"engine_not_running", "engine_identity_mismatch"}
    assert body["error"]["code"] != "order_rejected"
    combined = f"{captured.out}{captured.err}".lower()
    assert "<html" not in combined


def test_matching_lock_still_dispatches(engine: Engine) -> None:
    page = engine.page()
    page.start()
    try:
        assert main(_argv(engine.lock_path, "state")) == EXIT_CONFIRMED
    finally:
        page.stop()


class _Html404Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(404)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body>Not Found</body></html>")

    def log_message(self, format: str, *args: object) -> None:
        pass
