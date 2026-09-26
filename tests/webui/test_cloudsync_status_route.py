"""CloudSync status route regression tests.

Regression one-liners:
  - if no CloudSync scheduler configuration exists then GET status says off
  - if the configured endpoint has a recorded failed attempt then GET status says error
  - if the configured endpoint has completed attempts then GET status exposes the newest five
  - if live tracks are held for missing identity then GET identity-backlog counts them
  - if no tracks are held for missing identity then GET identity-backlog reads zero
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.cloudsync.test_hub_sync import _DEV_A, _T0
from tests.cloudsync.test_track_identity_collapse import _insert_identified_track


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
    # base_url pinned to a loopback host: SEC-01's host-allowlist middleware
    # (issue #2689) rejects TestClient's default `Host: testserver`, which
    # is not in the default allowlist (apps/webui/server/request_guard.py).
    return TestClient(app, base_url="http://127.0.0.1")


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


def test_status_exposes_update_required_for_wire_mismatch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MDT_CLOUDSYNC_SCHEDULER", "1")
    monkeypatch.setenv("MDT_CLOUDSYNC_HUB_URL", "https://hub.example.test")
    message = (
        'POST https://agentbox.<tailnet>:8870/api/v1/sync/hello -> HTTP 409: '
        '{"detail":{"code":"SYNC_WIRE_VERSION","message":"peer speaks sync wire v4, '
        'this machine speaks v3 (schema v14 vs v13). The synced row shapes differ, '
        'so no row may cross; upgrade whichever machine is on the lower wire version, '
        'then sync again."}}'
    )
    (tmp_path / "cloudsync-status.json").write_text(json.dumps({"results": [{
        "finished_at": "2026-09-15T06:00:00+00:00",
        "status": "error",
        "message": message,
        "pushed": 0,
        "pulled": 0,
    }]}), encoding="utf-8")
    with _client(tmp_path) as client:
        response = client.get("/api/v1/cloudsync/status")

    assert response.status_code == 200
    body = response.json()
    assert body["update_required"] == {
        "code": "SYNC_WIRE_VERSION",
        "local_wire_version": 3,
        "peer_wire_version": 4,
        "action": "install the latest Open DJ",
    }


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


# REQ: CLOUDSYNC-16
def test_identity_backlog_counts_held_inferred_tracks(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "state.db"
    conn = state_db.open_rw(db_path, apply_schema=True)
    try:
        _insert_identified_track(conn, "trk-held-a", title="a", updated_at=_T0, origin=_DEV_A)
        _insert_identified_track(conn, "trk-held-b", title="b", updated_at=_T0, origin=_DEV_A)
        _insert_identified_track(
            conn,
            "trk-hashed",
            title="hashed",
            content_hash="a" * 64,
            updated_at=_T0,
            origin=_DEV_A,
        )
        conn.commit()
    finally:
        conn.close()

    with _client(tmp_path) as client:
        response = client.get("/api/v1/cloudsync/identity-backlog")

    assert response.status_code == 200
    assert response.json() == {"unsyncable_inferred": 0, "hash_pending": 2}


def test_status_reports_hash_pending_and_quarantined_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "state.db"
    conn = state_db.open_rw(db_path, apply_schema=True)
    try:
        _insert_identified_track(conn, "trk-held-a", title="a", updated_at=_T0, origin=_DEV_A)
        _insert_identified_track(conn, "trk-held-b", title="b", updated_at=_T0, origin=_DEV_A)
        conn.commit()
    finally:
        conn.close()

    with _client(tmp_path) as client:
        response = client.get("/api/v1/cloudsync/status")

    assert response.status_code == 200
    body = response.json()
    assert body["hash_pending"] == 2
    assert body["quarantined"] == 0


def test_identity_backlog_is_zero_on_a_fully_identified_library(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "state.db"
    conn = state_db.open_rw(db_path, apply_schema=True)
    try:
        _insert_identified_track(
            conn,
            "trk-hashed",
            title="hashed",
            content_hash="a" * 64,
            updated_at=_T0,
            origin=_DEV_A,
        )
        conn.commit()
    finally:
        conn.close()

    with _client(tmp_path) as client:
        response = client.get("/api/v1/cloudsync/identity-backlog")

    assert response.status_code == 200
    assert response.json() == {"unsyncable_inferred": 0, "hash_pending": 0}
