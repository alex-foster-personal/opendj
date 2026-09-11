"""CloudSync status route regression tests.

Regression one-liners:
  - if no CloudSync scheduler configuration exists then GET status says off
  - if the configured endpoint has a recorded failed attempt then GET status says error
  - if the configured endpoint has completed attempts then GET status exposes the newest five
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend


def _client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "state" / "state.db"
    state_db.open_rw(db_path, apply_schema=True).close()
    app = create_app(
        backend=SqliteBackend(db_path),
        bind_host="127.0.0.1",
        hostname="cloudsync-status-test",
        state_db_path=str(db_path),
        mount_frontend=False,
    )
    return TestClient(app)


def test_status_is_off_when_cloudsync_is_not_configured(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MDT_CLOUDSYNC_SCHEDULER", raising=False)
    monkeypatch.delenv("MDT_CLOUDSYNC_HUB_URL", raising=False)
    with _client(tmp_path) as client:
        response = client.get("/api/v1/cloudsync/status")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["reason"] == "CloudSync is not configured."
    assert body["endpoint"] is None
    assert body["last_result"] is None
    assert body["recent_results"] == []


def test_status_exposes_a_recorded_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MDT_CLOUDSYNC_SCHEDULER", "1")
    monkeypatch.setenv("MDT_CLOUDSYNC_HUB_URL", "https://hub.example.test")
    (tmp_path / "cloudsync-status.json").write_text(json.dumps({"results": [{
        "finished_at": "2026-09-05T11:00:00+00:00",
        "status": "error",
        "message": "hub unavailable",
        "pushed": 0,
        "pulled": 0,
    }]}), encoding="utf-8")
    with _client(tmp_path) as client:
        response = client.get("/api/v1/cloudsync/status")

    assert response.status_code == 200
    body = response.json()
    # Configured by env, but no scheduler heartbeat exists in this test: the
    # truthful answer is configured-but-not-enabled (was a false-green).
    assert body["configured"] is True
    assert body["running"] is False
    assert body["enabled"] is False
    assert body["enabled_source"] == "env"
    assert body["endpoint_source"] == "env"
    assert body["endpoint"] == "https://hub.example.test"
    assert body["last_result"] == {"status": "error", "message": "hub unavailable"}
    assert body["recent_results"][0]["status"] == "error"


def test_status_keeps_only_the_newest_five_results(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MDT_CLOUDSYNC_SCHEDULER", "1")
    monkeypatch.setenv("MDT_CLOUDSYNC_HUB_URL", "https://hub.example.test")
    results = [
        {
            "finished_at": f"2026-09-05T11:00:0{i}+00:00",
            "status": "ok",
            "message": "completed",
            "pushed": i,
            "pulled": i + 1,
        }
        for i in range(6)
    ]
    (tmp_path / "cloudsync-status.json").write_text(
        json.dumps({"results": results}), encoding="utf-8"
    )
    with _client(tmp_path) as client:
        response = client.get("/api/v1/cloudsync/status")

    assert response.status_code == 200
    body = response.json()
    assert [result["pushed"] for result in body["recent_results"]] == [5, 4, 3, 2, 1]
    assert body["last_push_at"] == "2026-09-05T11:00:05+00:00"
    assert body["last_pull_at"] == "2026-09-05T11:00:05+00:00"
