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
from apps.sets.api import router
from apps.sets.recorder_service import RecorderService
from apps.sets.state import SetsState


@pytest.fixture
def recorder_client(tmp_path: Path):
    service = RecorderService(
        sets_root=tmp_path / "sets",
        db_path=tmp_path / "sets" / "sets.db",
        capture_enabled=False,
    )
    app = FastAPI()
    app.state.sets_recorder_service = service
    app.include_router(router)
    with TestClient(app) as client:
        yield client, service


def test_recorder_start_stop_settles_real_timeline(recorder_client):
    client, service = recorder_client
    session_id = "2026-07-22T22-00-00"
    body = {"session_id": session_id, "ffmpeg_device_idx": 0, "sources": []}

    started = client.post("/api/sets/recorder/start", json=body)
    duplicate = client.post("/api/sets/recorder/start", json=body)
    stopped = client.post(f"/api/sets/recorder/{session_id}/stop")

    assert started.status_code == 201
    assert started.json()["active"] is True
    assert duplicate.status_code == 409
    assert stopped.json() == {
        "active": False,
        "session_id": None,
        "pid": None,
        "owned": False,
        "recoverable": False,
    }
    session_dir = service.sets_root / session_id
    assert not (session_dir / "recorder.pid").exists()
    assert (session_dir / "manifest.json").exists()
    actions = [
        json.loads(line)["action"]
        for line in (session_dir / "timeline.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert actions[0] == "session_start"
    assert "session_end" in actions


def test_recover_finalizes_only_a_proven_stale_pid(recorder_client):
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

    assert status_response.json()["recoverable"] is True
    assert recovered.status_code == 200
    assert recovered.json()["active"] is False
    assert not (session_dir / "recorder.pid").exists()
    assert (session_dir / "manifest.json").exists()
    assert "session_end" in (session_dir / "timeline.jsonl").read_text(
        encoding="utf-8"
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


def test_stop_rejects_session_other_than_owned_recorder(recorder_client):
    client, _ = recorder_client
    session_id = "2026-07-22T22-10-00"
    client.post(
        "/api/sets/recorder/start",
        json={"session_id": session_id, "ffmpeg_device_idx": 0, "sources": []},
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
    app.include_router(router)
    session_id = "2026-07-22T22-20-00"

    with TestClient(app) as client:
        response = client.post(
            "/api/sets/recorder/start",
            json={"session_id": session_id, "ffmpeg_device_idx": 0, "sources": []},
        )
        assert response.status_code == 201

    session_dir = service.sets_root / session_id
    assert not (session_dir / "recorder.pid").exists()
    assert (session_dir / "manifest.json").exists()
