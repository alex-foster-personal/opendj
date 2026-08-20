"""Real HTTP and filesystem coverage for privacy-bounded visitor events."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

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
    assert paths[0].stat().st_mode & 0o777 == 0o600
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
