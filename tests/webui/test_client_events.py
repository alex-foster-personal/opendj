"""Real HTTP and filesystem coverage for privacy-bounded visitor events."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared import private_files
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend


def _payload() -> dict[str, object]:
    return {
        "client_event_id": "visitor-123",
        "kind": "page-view",
        "url": "https://agentbox.example.ts.net/performance",
        "path": "/performance",
        "referrer": "https://agentbox.example.ts.net/",
        "client_timestamp": "2026-08-17T09:00:00.000Z",
        "user_agent": "real-browser-user-agent",
        "language": "en-GB",
        "secure_context": True,
        "viewport_width": 1440,
        "viewport_height": 900,
    }


def test_page_view_writes_private_visitor_record(tmp_path: Path) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_event_log_dir=tmp_path,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/client-events",
            json=_payload(),
            headers={
                "Tailscale-User-Login": "dj@example.test",
                "Tailscale-User-Name": "Test DJ",
                "Cf-Access-Authenticated-User-Email": "friend@example.test",
            },
        )

    assert response.status_code == 202
    assert response.json()["stored"] is True
    paths = list(tmp_path.glob("webui-visitors-*.log"))
    assert len(paths) == 1
    # Owner-only, asked in whichever terms this OS actually enforces: mode
    # bits on POSIX, the file's ACL on Windows (where 0o600 means nothing).
    assert private_files.is_owner_only(paths[0])
    record = json.loads(paths[0].read_text(encoding="utf-8"))
    assert record["path"] == "/performance"
    assert record["tailscale_user_login"] == "dj@example.test"
    assert record["cloudflare_access_email"] == "friend@example.test"
    assert record["secure_context"] is True
    assert "?" not in record["url"]


def test_page_view_refuses_queries_in_logged_path_or_url(tmp_path: Path) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_event_log_dir=tmp_path,
    )
    payload = _payload()
    payload["path"] = "/performance?share=secret"
    with TestClient(app) as client:
        response = client.post("/api/v1/client-events", json=payload)

    assert response.status_code == 422
    assert list(tmp_path.iterdir()) == []

    payload = _payload()
    payload["url"] = "https://agentbox.example.ts.net/performance?share=secret"
    with TestClient(app) as client:
        response = client.post("/api/v1/client-events", json=payload)

    assert response.status_code == 422
    assert list(tmp_path.iterdir()) == []

    assert list(tmp_path.iterdir()) == []


def test_perf_span_stores_and_page_view_still_stores(tmp_path: Path) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_event_log_dir=tmp_path,
    )
    with TestClient(app) as client:
        page = client.post("/api/v1/client-events", json=_payload())
        perf = client.post(
            "/api/v1/client-events",
            json={
                "client_event_id": "perf-1",
                "kind": "perf-span",
                "name": "login-submit-to-library-usable",
                "duration_ms": 1500.0,
                "method": "client-telemetry in-app span",
                "client_timestamp": "2026-09-11T02:00:00.000Z",
            },
        )

    assert page.status_code == 202
    assert perf.status_code == 202
    paths = list(tmp_path.glob("webui-visitors-*.log"))
    assert len(paths) == 1
    lines = paths[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    kinds = {json.loads(line)["kind"] for line in lines}
    assert kinds == {"page-view", "perf-span"}


def test_perf_span_rejects_unknown_name(tmp_path: Path) -> None:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_event_log_dir=tmp_path,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/client-events",
            json={
                "client_event_id": "perf-bad",
                "kind": "perf-span",
                "name": "not-allowlisted",
                "duration_ms": 1.0,
                "method": "test",
                "client_timestamp": "2026-09-11T02:00:00.000Z",
            },
        )

    assert response.status_code == 422
    assert list(tmp_path.iterdir()) == []


pytestmark = pytest.mark.rb_parity
