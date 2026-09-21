"""Lifespan shutdown after a blocked audio-access probe (#2749 / PREFLIGHT-01).

Regression lines:
  - [if] an audio probe blocks [then] preflight fails audio-access, shutdown ends <10 s, [else stop]
"""

from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.webui.server import preflight_checks
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend

pytestmark = pytest.mark.requirement("PREFLIGHT-01")

SHUTDOWN_BUDGET_S = 10.0
CLIENT_TIMEOUT_S = 5.0
FIFO_SID = "b" * 40


def _get_with_deadline(client: TestClient, url: str, *, timeout: float) -> Response:
    result: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

    def _run() -> None:
        try:
            result.put(("ok", client.get(url)))
        except Exception as exc:  # noqa: BLE001
            result.put(("error", exc))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        outcome, payload = result.get(timeout=timeout)
    except queue.Empty:
        pytest.fail(f"GET {url} did not respond within {timeout}s")
    if outcome == "error":
        raise payload
    return payload  # type: ignore[return-value]


def _insert_track(state_path: Path, stable_id: str, file_path: str) -> None:
    conn = state_db.open_rw(state_path)
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, "
            "file_path, created_at, updated_at) "
            "VALUES (?, 'inferred', 210000, ?, '2026-01-01', '2026-01-01')",
            (stable_id, file_path),
        )
        conn.commit()
    finally:
        conn.close()


def test_lifespan_shutdown_completes_after_blocked_audio_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fifo = tmp_path / "blocked.fifo"
    os.mkfifo(fifo)
    monkeypatch.setattr(preflight_checks, "AUDIO_ACCESS_TIMEOUT_S", 0.5)

    state_path = tmp_path / "state.db"
    _insert_track(state_path, FIFO_SID, str(fifo))
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent-master.db")

    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
        mount_frontend=False,
        port=18735,
        frontend_port=19735,
        state_db_path=str(state_path),
    )
    start = time.monotonic()
    with TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1") as client:
        response = _get_with_deadline(client, "/api/v1/preflight", timeout=CLIENT_TIMEOUT_S)
        assert response.status_code == 200
        audio_row = next(c for c in response.json()["checks"] if c["id"] == "audio-access")
        assert audio_row["status"] == "fail"
    elapsed = time.monotonic() - start
    assert elapsed < SHUTDOWN_BUDGET_S, (
        f"lifespan shutdown took {elapsed:.1f}s after a blocked audio probe"
    )
