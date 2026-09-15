"""``opendj api`` parity for availability probe/status (issue #2588).

[if] the CLI drives the probe/status routes [then] it surfaces the routes' own fields, [else stop].
"""
from __future__ import annotations

import json
import socket
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from apps.engine_core.app import create_app
from apps.engine_core.availability_api import PROBE_PATH, STATUS_PATH
from apps.engine_core.config import EngineConfig
from apps.opendj_cli import api_cli
from apps.opendj_cli.__main__ import main
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from tests.waits import THREAD_HANG_GUARD_S, start_uvicorn_in_thread

pytestmark = pytest.mark.requirement("LIBM-09")


@pytest.fixture
def engine_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    db_path = data_dir / "state" / "state.db"
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"\x00")
    conn = state_db.open_rw(db_path)
    try:
        with StateWriter(conn, actor="test-cli-availability") as writer:
            writer.upsert_track(
                stable_id="c" * 40,
                stable_id_tier="inferred",
                title="t",
                artists=[],
                album=None,
                isrc=None,
                duration_ms=None,
                file_path=str(audio),
            )
        conn.commit()
    finally:
        conn.close()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])

    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MDT_LIBRARY_MODE", "local")
    monkeypatch.setenv("MUSIC_DJ_BACKEND_PORT", str(port))
    monkeypatch.setenv("MUSIC_DJ_FRONTEND_PORT", str(port + 1))

    app = create_app(EngineConfig(data_dir=data_dir))
    server, thread = start_uvicorn_in_thread(
        __import__("uvicorn").Config(app, log_level="warning"),
        what="the engine availability daemon",
        sockets=[listener],
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def test_opendj_api_drives_availability_probe_and_status(
    engine_daemon: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["api", "POST", PROBE_PATH, "--json", "{}"]) == api_cli.EXIT_OK
    started = time.monotonic()
    while time.monotonic() - started < THREAD_HANG_GUARD_S:
        response = httpx.get(f"{engine_daemon}{STATUS_PATH}", timeout=5.0)
        response.raise_for_status()
        status = response.json()
        if status.get("complete") and status.get("pending") == 0:
            assert status["present"] == 1
            break
        time.sleep(0.05)
    else:
        raise AssertionError("availability status never completed")

    capsys.readouterr()
    assert main(["api", "GET", STATUS_PATH]) == api_cli.EXIT_OK
    captured = capsys.readouterr()
    cli_status = json.loads(captured.out)
    assert cli_status["present"] == 1
