"""Issue #13164: daemon host and origin guard regressions before routes run.

[if] Host or Origin fails the guard [then] command routes do not run, [else stop].
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend

pytestmark = pytest.mark.requirement("PLATFORM-13484")

LOOPBACK_BASE_URL = "http://127.0.0.1"
BACKEND_PORT = 8698
FRONTEND_PORT = 9412
PLAY = {"single": {"type": "play", "deck": 1, "playing": True}}


def _client() -> TestClient:
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=True,
        port=BACKEND_PORT,
        frontend_port=FRONTEND_PORT,
    )
    return TestClient(app, base_url=LOOPBACK_BASE_URL)


def _open_performance_page(client: TestClient) -> None:
    published = client.put("/api/v1/state/ui-mirror", json={"client_open": True})
    assert published.status_code == 202


def _assert_queue_empty(client: TestClient) -> None:
    nxt = client.get("/api/v1/commands/next")
    assert nxt.status_code == 200
    assert nxt.json() is None


def test_allowed_host_and_origin_mutates_through_commands() -> None:
    """[if] allowed host and origin on POST [then] command queues, [else stop]."""
    client = _client()
    _open_performance_page(client)
    origin = f"http://127.0.0.1:{FRONTEND_PORT}"
    post_box: dict[str, Any] = {}

    def post_in_background() -> None:
        post_box["response"] = client.post(
            "/api/v1/commands",
            json=PLAY,
            headers={"Origin": origin},
        )

    thread = threading.Thread(target=post_in_background, daemon=True)
    thread.start()
    nxt = client.get("/api/v1/commands/next", params={"wait_ms": 5000})
    assert nxt.status_code == 200
    claimed = nxt.json()
    assert claimed is not None
    assert isinstance(claimed.get("id"), str)
    result = client.post(
        f"/api/v1/commands/{claimed['id']}/result",
        json={"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}},
    )
    assert result.status_code == 202
    thread.join(timeout=20)
    post = post_box["response"]
    assert post.status_code != 403 or post.json().get("code") not in {
        "HOST_NOT_ALLOWED",
        "ORIGIN_NOT_ALLOWED",
    }


def test_untrusted_host_blocks_before_commands_route() -> None:
    """[if] untrusted Host on POST [then] HOST_NOT_ALLOWED and empty queue, [else stop]."""
    client = _client()
    health = client.get("/api/v1/health", headers={"Host": "evil.example"})
    assert health.status_code == 403
    assert health.json()["code"] == "HOST_NOT_ALLOWED"

    _open_performance_page(client)
    post = client.post(
        "/api/v1/commands",
        json=PLAY,
        headers={"Host": "evil.example"},
    )
    assert post.status_code == 403
    assert post.json()["code"] == "HOST_NOT_ALLOWED"
    _assert_queue_empty(client)


def test_mismatched_origin_blocks_before_commands_route() -> None:
    """[if] foreign Origin on POST [then] ORIGIN_NOT_ALLOWED and empty queue, [else stop]."""
    client = _client()
    _open_performance_page(client)
    post = client.post(
        "/api/v1/commands",
        json=PLAY,
        headers={"Origin": "https://evil.example"},
    )
    assert post.status_code == 403
    assert post.json()["code"] == "ORIGIN_NOT_ALLOWED"
    _assert_queue_empty(client)
