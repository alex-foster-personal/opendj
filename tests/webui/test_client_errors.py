"""Real HTTP and filesystem coverage for browser error capture."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend

#: The request guard (#2689) refuses TestClient's default ``Host: testserver``.
_LOOPBACK = "http://127.0.0.1"


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
    with TestClient(app, base_url=_LOOPBACK) as client:
        response = client.post("/api/v1/client-errors", json=_payload())

    assert response.status_code == 202
    assert response.json()["stored"] is True
    paths = list(tmp_path.glob("webui-client-errors-*.log"))
    assert len(paths) == 1
    records = paths[0].read_text(encoding="utf-8").splitlines()
    assert len(records) == 1
    record = json.loads(records[0])
    assert record["event_id"] == response.json()["event_id"]
    assert record["error_id"].startswith("eid-")
    assert "host" in record
    assert "build_sha" in record
    assert record["secure_context"] is False
    assert record["audio_worklet_available"] is False
    assert "createProcessor" in record["stack"]
    assert record["received_at"].endswith("Z")


def test_list_client_errors_includes_stack_and_url(tmp_path: Path) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    with TestClient(app, base_url=_LOOPBACK) as client:
        created = client.post("/api/v1/client-errors", json=_payload())
        event_id = created.json()["event_id"]
        rows = client.get("/api/v1/client-errors").json()

    row = next(record for record in rows if record["event_id"] == event_id)
    assert "createProcessor" in row["stack"]
    assert row["url"] == _payload()["url"]


def test_client_error_accepts_browser_capture_kinds(tmp_path: Path) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    for kind in (
        "console-error",
        "console-warn",
        "resource-error",
        "csp-violation",
        "webview-console",
        "webview-navigation",
    ):
        payload = _payload()
        payload["kind"] = kind
        payload["client_event_id"] = f"{kind}-probe"
        with TestClient(app, base_url=_LOOPBACK) as client:
            response = client.post("/api/v1/client-errors", json=payload)
        assert response.status_code == 202, kind


def test_client_error_rejects_unbounded_stack(tmp_path: Path) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    payload = _payload()
    payload["stack"] = "x" * 32769
    with TestClient(app, base_url=_LOOPBACK) as client:
        response = client.post("/api/v1/client-errors", json=payload)

    assert response.status_code == 422
    assert list(tmp_path.iterdir()) == []


def test_client_error_triage_hides_decided_event_without_rewriting_daily_log(
    tmp_path: Path,
) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    with TestClient(app, base_url=_LOOPBACK) as client:
        created = client.post("/api/v1/client-errors", json=_payload())
        event_id = created.json()["event_id"]
        daily_log = next(tmp_path.glob("webui-client-errors-*.log"))
        daily_before = daily_log.read_bytes()

        untriaged = client.get("/api/v1/client-errors?untriaged=1")
        triaged = client.patch(
            f"/api/v1/client-errors/{event_id}",
            json={"disposition": "no-fix", "ref": "known browser limitation"},
        )
        after = client.get("/api/v1/client-errors?untriaged=1")

    assert untriaged.status_code == 200
    assert [record["event_id"] for record in untriaged.json()] == [event_id]
    assert triaged.status_code == 200
    assert triaged.json()["event_id"] == event_id
    assert after.json() == []
    assert daily_log.read_bytes() == daily_before
    sidecars = list(tmp_path.glob("webui-client-errors-*.triage.jsonl"))
    assert len(sidecars) == 1
    assert json.loads(sidecars[0].read_text(encoding="utf-8")) == {
        "disposition": "no-fix",
        "event_id": event_id,
        "ref": "known browser limitation",
    }

pytestmark = pytest.mark.rb_parity


def test_client_error_log_line_is_warning_not_a_second_sentry_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """if the route logs a reported browser error at ERROR then broken: warning_log
    forwards ERROR records to Sentry, so the one error would be sent twice."""
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_error_log_dir=tmp_path,
    )
    route_logger = "apps.webui.server.routes.client_errors"
    with (
        caplog.at_level("WARNING", logger=route_logger),
        TestClient(app, base_url=_LOOPBACK) as client,
    ):
        response = client.post("/api/v1/client-errors", json=_payload())
    assert response.status_code == 202
    lines = [r for r in caplog.records if r.getMessage().startswith("browser error")]
    assert [r.levelname for r in lines] == ["WARNING"]
