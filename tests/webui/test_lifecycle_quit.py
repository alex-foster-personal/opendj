"""POST /api/v1/lifecycle/quit (INSTALL-21, OPS-07)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend

pytestmark = pytest.mark.requirement("INSTALL-21")

LOOPBACK_BASE_URL = "http://127.0.0.1:8686"


@pytest.fixture
def lifecycle_client(seed_backend: InMemoryBackend, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from apps.webui.server import rb_vendor

    monkeypatch.setattr(rb_vendor, "playlist_order_index", lambda: {})
    app = create_app(
        backend=seed_backend,
        bind_host="127.0.0.1",
        hostname="test-host",
        port=18697,
        frontend_port=19411,
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
    )
    with TestClient(app, base_url=LOOPBACK_BASE_URL, client=("127.0.0.1", 50123)) as client:
        yield client


@pytest.mark.requirement("INSTALL-21")
def test_force_quit_from_loopback_succeeds(lifecycle_client: TestClient) -> None:
    """[if] loopback POST with force true [then] 200 and ok, [else stop]."""
    response = lifecycle_client.post("/api/v1/lifecycle/quit", json={"force": True})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.requirement("INSTALL-21")
def test_force_flag_is_required(lifecycle_client: TestClient) -> None:
    """[if] force is omitted or false [then] 422, [else stop]."""
    assert lifecycle_client.post("/api/v1/lifecycle/quit", json={"force": False}).status_code == 422
    assert lifecycle_client.post("/api/v1/lifecycle/quit", json={}).status_code == 422


@pytest.mark.requirement("INSTALL-21")
def test_non_loopback_peer_is_refused(seed_backend: InMemoryBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] caller is not the local operator [then] 403, [else stop]."""
    from apps.webui.server import rb_vendor

    monkeypatch.setattr(rb_vendor, "playlist_order_index", lambda: {})
    app = create_app(backend=seed_backend, bind_host="0.0.0.0", hostname="test-host")
    with TestClient(app, base_url=LOOPBACK_BASE_URL, client=("203.0.113.7", 50123)) as remote_client:
        response = remote_client.post("/api/v1/lifecycle/quit", json={"force": True})
        assert response.status_code == 403


@pytest.mark.requirement("INSTALL-21")
def test_second_force_quit_is_idempotent(lifecycle_client: TestClient) -> None:
    """[if] force quit is posted twice [then] both succeed, [else stop]."""
    first = lifecycle_client.post("/api/v1/lifecycle/quit", json={"force": True})
    second = lifecycle_client.post("/api/v1/lifecycle/quit", json={"force": True})
    assert first.status_code == 200
    assert second.status_code == 200
