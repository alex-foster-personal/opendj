"""SEC-01: daemon host allowlist and mutating-origin guard (issue #2689)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.request_guard import (
    RequestGuardBindRefused,
    assert_request_guard_bind_allowed,
)
from apps.webui.server.share_gate import AUTH_TOKEN, ShareConfig

pytestmark = pytest.mark.requirement("SEC-01")

LOOPBACK_BASE_URL = "http://127.0.0.1"
SHARE_HOST = "dj.example"
SHARE_TOKEN = "share-secret-token"
TAILNET_HOST = "agentbox.example-tailnet.ts.net"


def _client(
    *,
    enable_cors: bool = True,
    frontend_port: int = 9411,
    backend_port: int = 8697,
    share_config: ShareConfig | None = None,
) -> TestClient:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=enable_cors,
        port=backend_port,
        frontend_port=frontend_port,
        share_config=share_config,
    )
    return TestClient(app, base_url=LOOPBACK_BASE_URL)


def test_get_rejects_foreign_host() -> None:
    client = _client()
    resp = client.get("/api/v1/health", headers={"Host": "evil.example"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "HOST_NOT_ALLOWED"


def test_post_rejects_foreign_origin() -> None:
    client = _client()
    resp = client.post(
        "/api/v1/commands",
        json={"single": {"type": "play", "deck": 1, "playing": True}},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "ORIGIN_NOT_ALLOWED"


def test_post_allows_frontend_origin() -> None:
    frontend_port = 9411
    client = _client(frontend_port=frontend_port)
    resp = client.post(
        "/api/v1/commands",
        json={"single": {"type": "play", "deck": 1, "playing": True}},
        headers={"Origin": f"http://127.0.0.1:{frontend_port}"},
    )
    assert resp.status_code != 403 or resp.json().get("code") not in {
        "ORIGIN_NOT_ALLOWED",
        "HOST_NOT_ALLOWED",
    }


def test_post_allows_backend_origin() -> None:
    backend_port = 8697
    client = _client(backend_port=backend_port)
    resp = client.post(
        "/api/v1/commands",
        json={"single": {"type": "play", "deck": 1, "playing": True}},
        headers={"Origin": f"http://127.0.0.1:{backend_port}"},
    )
    assert resp.status_code != 403 or resp.json().get("code") not in {
        "ORIGIN_NOT_ALLOWED",
        "HOST_NOT_ALLOWED",
    }


def test_mutate_without_origin_allowed() -> None:
    client = _client()
    post = client.post(
        "/api/v1/commands",
        json={"single": {"type": "play", "deck": 1, "playing": True}},
    )
    patch = client.patch(
        f"/api/v1/tracks/{'a' * 40}",
        json={"rating": 5},
    )
    for resp in (post, patch):
        assert resp.status_code != 403 or resp.json().get("code") not in {
            "ORIGIN_NOT_ALLOWED",
            "HOST_NOT_ALLOWED",
        }


def test_share_host_still_gated() -> None:
    client = _client(
        share_config=ShareConfig(
            host=SHARE_HOST,
            auth=AUTH_TOKEN,
            token=SHARE_TOKEN,
            read_only=True,
        )
    )
    health = client.get("/api/v1/health", headers={"Host": SHARE_HOST})
    assert health.status_code == 200
    settings = client.get("/api/v1/settings", headers={"Host": SHARE_HOST})
    assert settings.status_code == 401
    assert settings.json()["code"] == "SHARE_UNAUTHORIZED"


def test_tailnet_host_requires_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = _client()
    resp = blocked.get(
        "/api/v1/health",
        headers={"Host": TAILNET_HOST},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "HOST_NOT_ALLOWED"

    monkeypatch.setenv("MUSIC_DJ_ALLOWED_HOSTS", TAILNET_HOST)
    allowed = _client()
    ok = allowed.get(
        "/api/v1/health",
        headers={"Host": TAILNET_HOST},
    )
    assert ok.status_code == 200


def test_startup_refuses_non_loopback_without_allowlist() -> None:
    with pytest.raises(RequestGuardBindRefused):
        assert_request_guard_bind_allowed("0.0.0.0", {})
    assert_request_guard_bind_allowed(
        "0.0.0.0",
        {"MUSIC_DJ_ALLOWED_HOSTS": TAILNET_HOST},
    )


def test_origin_guard_disabled_when_cors_disabled() -> None:
    client = _client(enable_cors=False)
    resp = client.post(
        "/api/v1/commands",
        json={"single": {"type": "play", "deck": 1, "playing": True}},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.json().get("code") != "ORIGIN_NOT_ALLOWED"
