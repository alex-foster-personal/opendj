"""Real HTTP and filesystem coverage for browser error capture."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend


def _payload() -> dict[str, object]:
    return {
        "client_event_id": "browser-123",
        "kind": "ui-error",
        "message": "AudioWorklet is unavailable so Signalsmith cannot start",
        "name": "Error",
        "stack": "Error: AudioWorklet is unavailable\n    at createProcessor (audio.ts:42)",
        "url": "https://agentbox.example.ts.net/performance",
        "client_timestamp": "2026-08-17T09:00:00.000Z",
        "user_agent": "real-browser-user-agent",
        "secure_context": False,
        "audio_worklet_available": False,
        "context": {"source": "toast", "deck": 4},
    }


def test_client_error_writes_full_details_to_separate_log(tmp_path: Path) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    with TestClient(app) as client:
        response = client.post("/api/v1/client-errors", json=_payload())

    assert response.status_code == 202
    assert response.json()["stored"] is True
    paths = list(tmp_path.glob("webui-client-errors-*.log"))
    assert len(paths) == 1
    records = paths[0].read_text(encoding="utf-8").splitlines()
    assert len(records) == 1
    record = json.loads(records[0])
    assert record["event_id"] == response.json()["event_id"]
    assert record["secure_context"] is False
    assert record["audio_worklet_available"] is False
    assert "createProcessor" in record["stack"]
    assert record["received_at"].endswith("Z")


def test_client_error_rejects_unbounded_stack(tmp_path: Path) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    payload = _payload()
    payload["stack"] = "x" * 32769
    with TestClient(app) as client:
        response = client.post("/api/v1/client-errors", json=payload)

    assert response.status_code == 422
    assert list(tmp_path.iterdir()) == []

pytestmark = pytest.mark.rb_parity
