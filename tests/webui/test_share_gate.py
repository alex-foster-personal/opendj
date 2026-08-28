"""Share-host gate: token + read-only, loopback stays full access."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend

SHARE_HOST = "dj.example"
TOKEN = "share-secret-token"


@pytest.fixture
def share_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MUSIC_DJ_SHARE_HOST", SHARE_HOST)
    monkeypatch.setenv("MUSIC_DJ_SHARE_TOKEN", TOKEN)
    monkeypatch.setenv("MUSIC_DJ_SHARE_READ_ONLY", "1")
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
    )
    return TestClient(app)


def test_loopback_stays_open_without_token(share_client: TestClient) -> None:
    resp = share_client.get("/api/v1/health")
    assert resp.status_code == 200


def test_share_host_without_token_is_401(share_client: TestClient) -> None:
    resp = share_client.get(
        "/api/v1/settings",
        headers={"Host": SHARE_HOST},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "SHARE_UNAUTHORIZED"


def test_share_host_with_token_can_read(share_client: TestClient) -> None:
    resp = share_client.get(
        "/api/v1/settings",
        headers={
            "Host": SHARE_HOST,
            "Authorization": f"Bearer {TOKEN}",
        },
    )
    assert resp.status_code == 200


def test_share_host_is_read_only(share_client: TestClient) -> None:
    resp = share_client.patch(
        f"/api/v1/tracks/{'a' * 40}",
        headers={
            "Host": SHARE_HOST,
            "Authorization": f"Bearer {TOKEN}",
        },
        json={"rating": 5},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "SHARE_READ_ONLY"


def test_health_is_exempt_on_share_host(share_client: TestClient) -> None:
    resp = share_client.get("/api/v1/health", headers={"Host": SHARE_HOST})
    assert resp.status_code == 200
