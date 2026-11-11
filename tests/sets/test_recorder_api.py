"""REC HTTP acceptance tests using the genuine recorder lifecycle.

[if] REC starts twice [then ⛔️] a second session or PID is created.
[if] REC stops [then ⛔️] the PID survives or manifest/session_end is absent.
[if] the router shuts down [then ⛔️] its owned recorder remains live.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sets import record as record_mod
from apps.sets.recorder_service import RecorderService
from apps.sets.state import SetsState
from tests.sets.conftest import mount_sets_router


@pytest.fixture
def recorder_client(tmp_path: Path):
    service = RecorderService(
        sets_root=tmp_path / "sets",
        db_path=tmp_path / "sets" / "sets.db",
        capture_enabled=False,
    )
    app = FastAPI()
    app.state.sets_recorder_service = service
    mount_sets_router(app)
    with TestClient(app) as client:
        yield client, service


@pytest.fixture
def dual_prefix_recorder_client(tmp_path: Path):
    service = RecorderService(
        sets_root=tmp_path / "sets",
        db_path=tmp_path / "sets" / "sets.db",
        capture_enabled=False,
    )
    app = FastAPI()
    app.state.sets_recorder_service = service
    mount_sets_router(app, v1_alias=True)
    with TestClient(app) as client:
        yield client, service


def _idle_recorder_status_json(service: RecorderService) -> dict[str, object]:
    return {
        "active": False,
        "session_id": None,
        "pid": None,
        "owned": False,
        "recoverable": False,
        "capture": "none",
        "capture_error": None,
        "capture_source": None,
        "recordings_dir": str(service.sets_root),
        "peak_dbfs": None,
        "peak_dbfs_recent": None,
        "clipped_samples": None,
        "clip_events": None,
        "elapsed_s": None,
        "audio_bytes": None,
    }


def _external_recorder_status_json(
    service: RecorderService,
    *,
    session_id: str,
    pid: int,
    recoverable: bool,
) -> dict[str, object]:
    return {
        "active": True,
        "session_id": session_id,
        "pid": pid,
        "owned": False,
        "recoverable": recoverable,
        "capture": "unknown",
        "capture_error": None,
        "capture_source": None,
        "recordings_dir": str(service.sets_root),
        "peak_dbfs": None,
        "peak_dbfs_recent": None,
        "clipped_samples": None,
        "clip_events": None,
        "elapsed_s": None,
        "audio_bytes": None,
    }


@pytest.mark.requirement("SET-11818")
def test_v1_recorder_start_shares_service_with_legacy_prefix(dual_prefix_recorder_client) -> None:
    """[if] REC starts on /api/v1 [then] legacy GET shows same active session, [else stop]."""
    client, _service = dual_prefix_recorder_client
    session_id = "2026-11-11T21-30-00"
    body = {"session_id": session_id, "source": "external", "ffmpeg_device_idx": 0, "sources": []}

    started = client.post("/api/v1/sets/recorder/start", json=body)
    assert started.status_code == 201
    assert started.json()["active"] is True
    assert started.json()["session_id"] == session_id

    legacy = client.get("/api/sets/recorder")
    v1 = client.get("/api/v1/sets/recorder")
    assert legacy.status_code == 200
    assert v1.status_code == 200
    legacy_body = legacy.json()
    v1_body = v1.json()
    assert legacy_body["active"] is True
    assert v1_body["active"] is True
    assert legacy_body["session_id"] == session_id == v1_body["session_id"]
    assert legacy_body["pid"] == v1_body["pid"]

    duplicate = client.post("/api/sets/recorder/start", json=body)
    assert duplicate.status_code == 409

    stopped = client.post(f"/api/v1/sets/recorder/{session_id}/stop")
    assert stopped.status_code == 200


def test_recorder_start_stop_settles_real_timeline(recorder_client):
    client, service = recorder_client
    session_id = "2026-07-22T22-00-00"
    body = {"session_id": session_id, "source": "external", "ffmpeg_device_idx": 0, "sources": []}

    started = client.post("/api/sets/recorder/start", json=body)
    duplicate = client.post("/api/sets/recorder/start", json=body)
    stopped = client.post(f"/api/sets/recorder/{session_id}/stop")

    assert started.status_code == 201
    assert started.json()["active"] is True
    assert started.json()["capture"] == "none"
    assert duplicate.status_code == 409
    assert stopped.json() == _idle_recorder_status_json(service)
    session_dir = service.sets_root / session_id
    assert not (session_dir / "recorder.pid").exists()
    assert (session_dir / "manifest.json").exists()
    actions = [
        json.loads(line)["action"] for line in (session_dir / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert actions[0] == "session_start"
    assert "session_end" in actions


@pytest.mark.requirement("SET-12")
def test_recover_finalizes_only_a_proven_stale_pid(recorder_client):
    """[if] an external recorder PID is stale [then] status reports recoverable, [else stop]."""
    client, service = recorder_client
    session_id = "2026-07-22T22-30-00"
    record_mod.start(
        session_id=session_id,
        config=record_mod.RecorderConfig(sources=(), capture_disabled=True),
        sets_root=service.sets_root,
        state=SetsState(db_path=service.db_path),
    )
    stale_pid = 2_000_000_000
    session_dir = service.sets_root / session_id
    (session_dir / "recorder.pid").write_text(str(stale_pid), encoding="utf-8")

    status_response = client.get("/api/sets/recorder")
    recovered = client.post(
        f"/api/sets/recorder/{session_id}/recover",
        json={"expected_pid": stale_pid},
    )

    assert status_response.status_code == 200
    assert status_response.json() == _external_recorder_status_json(
        service,
        session_id=session_id,
        pid=stale_pid,
        recoverable=True,
    )
    assert recovered.status_code == 200
    assert recovered.json()["active"] is False
    assert not (session_dir / "recorder.pid").exists()
    assert (session_dir / "manifest.json").exists()
    assert "session_end" in (session_dir / "timeline.jsonl").read_text(encoding="utf-8")


@pytest.mark.requirement("SET-12")
def test_recorder_status_reports_live_external_session(recorder_client):
    """[if] a live external recorder PID owns a session [then] status reports it, [else stop]."""
    client, service = recorder_client
    session_id = "2026-07-22T22-35-00"
    record_mod.start(
        session_id=session_id,
        config=record_mod.RecorderConfig(sources=(), capture_disabled=True),
        sets_root=service.sets_root,
        state=SetsState(db_path=service.db_path),
    )
    session_dir = service.sets_root / session_id
    live_pid = int((session_dir / "recorder.pid").read_text(encoding="utf-8"))

    response = client.get("/api/sets/recorder")

    assert response.status_code == 200
    assert response.json() == _external_recorder_status_json(
        service,
        session_id=session_id,
        pid=live_pid,
        recoverable=False,
    )


def test_recover_rejects_a_live_external_pid(recorder_client):
    client, service = recorder_client
    session_id = "2026-07-22T22-40-00"
    record_mod.start(
        session_id=session_id,
        config=record_mod.RecorderConfig(sources=(), capture_disabled=True),
        sets_root=service.sets_root,
        state=SetsState(db_path=service.db_path),
    )

    response = client.post(
        f"/api/sets/recorder/{session_id}/recover",
        json={"expected_pid": os.getpid()},
    )

    assert response.status_code == 409
    assert "still running" in response.json()["detail"]


def test_pid_is_running_short_circuits_non_positive_without_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] pid <= 0 [then] _pid_is_running is False without calling pid_is_alive [else stop]."""
    from apps.sets import recorder_service

    calls: list[int] = []

    def _spy(pid: int) -> bool:
        calls.append(pid)
        return True

    monkeypatch.setattr(recorder_service, "pid_is_alive", _spy)
    assert recorder_service._pid_is_running(0) is False
    assert recorder_service._pid_is_running(-1) is False
    assert calls == []
    assert recorder_service._pid_is_running(1) is True
    assert calls == [1]


def test_stop_rejects_session_other_than_owned_recorder(recorder_client):
    client, _ = recorder_client
    session_id = "2026-07-22T22-10-00"
    client.post(
        "/api/sets/recorder/start",
        json={"session_id": session_id, "source": "external", "ffmpeg_device_idx": 0, "sources": []},
    )

    response = client.post("/api/sets/recorder/2026-07-22T22-11-00/stop")

    assert response.status_code == 409
    assert session_id in response.json()["detail"]


def test_router_shutdown_settles_owned_recorder(tmp_path: Path):
    service = RecorderService(
        sets_root=tmp_path / "sets",
        db_path=tmp_path / "sets" / "sets.db",
        capture_enabled=False,
    )
    app = FastAPI()
    app.state.sets_recorder_service = service
    mount_sets_router(app)
    session_id = "2026-07-22T22-20-00"

    with TestClient(app) as client:
        response = client.post(
            "/api/sets/recorder/start",
            json={"session_id": session_id, "source": "external", "ffmpeg_device_idx": 0, "sources": []},
        )
        assert response.status_code == 201

    session_dir = service.sets_root / session_id
    assert not (session_dir / "recorder.pid").exists()
    assert (session_dir / "manifest.json").exists()


@pytest.mark.requirement("SET-12")
def test_recorder_status_route_answers_when_idle(recorder_client):
    """[if] no recorder PID exists [then] GET /api/sets/recorder is idle JSON, [else stop]."""
    client, service = recorder_client

    response = client.get("/api/sets/recorder")

    assert response.status_code == 200
    assert response.json() == _idle_recorder_status_json(service)


def test_record_status_still_takes_its_public_state_keyword():
    """[if] record.status rejects `state=` [then ⛔️] broken.

    The route test above catches this through the HTTP layer; this one names
    the contract directly, so the failure message points at the signature
    instead of at a 500.
    """
    record_mod.status(sets_root=None, state=SetsState(db_path=Path("/nonexistent.db")))
