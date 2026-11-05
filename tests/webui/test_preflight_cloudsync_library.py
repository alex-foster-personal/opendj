"""CloudSync-aware empty-library preflight (issue #2574).

Configured spokes with no successful pull yet must not report library-attached
as fail or auto-open import; unconfigured empty and post-ok empty stay fail.

[if] a configured spoke with no pull yet still auto-fails the library row [then] broken, [else stop].
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.engine_core.setup import record as setup_record
from apps.shared.state import db as state_db
from apps.sync_hub import config as sync_config
from apps.sync_hub import status as sync_status
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.preflight_checks import (
    LIBRARY_ATTACHED_REMEDIATION,
    LIBRARY_ATTACHED_WAITING_SYNC_REMEDIATION,
)

pytestmark = pytest.mark.requirement("PREFLIGHT-8595")

_HUB = "https://hub.example-tailnet.ts.net:8871"
_NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)


def _build_client(monkeypatch: pytest.MonkeyPatch, data_dir: Path, *, create_state_db: bool = True) -> TestClient:
    state_path = data_dir / "state" / "state.db"
    if create_state_db:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_db.open_rw(state_path).close()
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", data_dir / "absent-master.db")
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
        state_db_path=str(state_path),
    )
    return TestClient(app, raise_server_exceptions=False)


def _check(body: dict, check_id: str) -> dict:
    matches = [c for c in body["checks"] if c["id"] == check_id]
    assert len(matches) == 1, f"expected exactly one {check_id!r} row, got {matches}"
    return matches[0]


def _insert_track(state_path: Path, stable_id: str) -> None:
    conn = state_db.open_rw(state_path)
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, "
            "file_path, created_at, updated_at) "
            "VALUES (?, 'inferred', 210000, '/music/present.mp3', '2026-01-01', '2026-01-01')",
            (stable_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _configure_cloudsync(data_dir: Path) -> None:
    sync_config.write_config(
        data_dir,
        sync_config.CloudSyncConfig(enabled=True, hub_url=_HUB, machine_name=None),
    )


def _write_sync_result(data_dir: Path, *, status: str) -> None:
    sync_status.write_result(
        data_dir,
        sync_status.SyncResult(
            finished_at=_NOW.isoformat(),
            status=status,
            message=f"test {status}",
            pushed=0,
            pulled=0,
        ),
    )


def test_unconfigured_empty_library_stays_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] an unconfigured empty library is pending-waiting [then] broken, [else stop]."""
    client = _build_client(monkeypatch, tmp_path)
    body = client.get("/api/v1/preflight").json()
    library = _check(body, "library-attached")
    assert library["status"] == "fail"
    assert library["severity"] == "advisory"
    assert body["status"] == "pass"
    assert LIBRARY_ATTACHED_REMEDIATION in library["remediation"]


def test_configured_empty_without_journal_is_pending_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_cloudsync(tmp_path)
    client = _build_client(monkeypatch, tmp_path)
    body = client.get("/api/v1/preflight").json()
    library = _check(body, "library-attached")
    assert library["status"] == "pending"
    assert library["severity"] == "advisory"
    assert body["status"] == "pass"
    assert "waiting for CloudSync" in library["detail"]
    assert "hub" in library["user_detail"].lower() or "sync" in library["user_detail"].lower()
    assert not library["remediation"].startswith("Import your music to get started.")


def test_configured_empty_after_error_stays_pending_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_cloudsync(tmp_path)
    _write_sync_result(tmp_path, status="error")
    client = _build_client(monkeypatch, tmp_path)
    library = _check(client.get("/api/v1/preflight").json(), "library-attached")
    assert library["status"] == "pending"
    assert "waiting for CloudSync" in library["detail"]


def test_configured_empty_after_deferred_stays_pending_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_cloudsync(tmp_path)
    _write_sync_result(tmp_path, status="deferred")
    client = _build_client(monkeypatch, tmp_path)
    library = _check(client.get("/api/v1/preflight").json(), "library-attached")
    assert library["status"] == "pending"
    assert "waiting for CloudSync" in library["detail"]


def test_configured_empty_after_ok_pull_stays_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] an empty hub after a successful pull stays waiting forever [then] broken, [else stop]."""
    _configure_cloudsync(tmp_path)
    _write_sync_result(tmp_path, status="ok")
    client = _build_client(monkeypatch, tmp_path)
    library = _check(client.get("/api/v1/preflight").json(), "library-attached")
    assert library["status"] == "fail"
    assert library["severity"] == "advisory"
    assert LIBRARY_ATTACHED_REMEDIATION in library["remediation"]


def test_dismissed_wins_over_cloudsync_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_cloudsync(tmp_path)
    setup_record.set_dismissed(tmp_path, True)
    client = _build_client(monkeypatch, tmp_path)
    library = _check(client.get("/api/v1/preflight").json(), "library-attached")
    assert library["status"] == "pending"
    assert "dismissed" in library["detail"]
    assert "waiting for CloudSync" not in library["detail"]


def test_malformed_journal_falls_back_to_unconfigured_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_cloudsync(tmp_path)
    (tmp_path / "cloudsync-status.json").write_text("{ not json", encoding="utf-8")
    response = _build_client(monkeypatch, tmp_path).get("/api/v1/preflight")
    assert response.status_code == 200
    library = _check(response.json(), "library-attached")
    assert library["status"] == "fail"
    assert "waiting for CloudSync" not in library["detail"]


def test_configured_with_tracks_stays_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_cloudsync(tmp_path)
    state_path = tmp_path / "state" / "state.db"
    _insert_track(state_path, "a" * 40)
    client = _build_client(monkeypatch, tmp_path, create_state_db=False)
    library = _check(client.get("/api/v1/preflight").json(), "library-attached")
    assert library["status"] == "pass"
    assert "1 tracks" in library["detail"]


def test_configured_no_state_db_is_pending_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_cloudsync(tmp_path)
    client = _build_client(monkeypatch, tmp_path, create_state_db=False)
    library = _check(client.get("/api/v1/preflight").json(), "library-attached")
    assert library["status"] == "pending"
    assert "waiting for CloudSync" in library["detail"]
    assert LIBRARY_ATTACHED_WAITING_SYNC_REMEDIATION in library["remediation"]
