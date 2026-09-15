"""Shell navigation HTTP contract (issue #2866, AGENT-12)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.routes.shell import router as shell_router
from apps.webui.server.routes.state import router as state_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(shell_router, prefix="/api/v1")
    app.include_router(state_router, prefix="/api/v1")
    return TestClient(app)


def test_post_navigate_accepts_performance_route() -> None:
    with _client() as client:
        response = client.post("/api/v1/shell/navigate", json={"route": "/performance"})

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["route"] == "/performance"
    assert isinstance(body["id"], str) and body["id"]


def test_pending_poll_does_not_consume() -> None:
    with _client() as client:
        posted = client.post("/api/v1/shell/navigate", json={"route": "/performance"})
        first = client.get("/api/v1/shell/navigate/pending")
        second = client.get("/api/v1/shell/navigate/pending")

    pending = posted.json()
    assert first.json() == {"pending": {"id": pending["id"], "route": "/performance"}}
    assert second.json() == first.json()


def test_ack_clears_pending_when_id_matches() -> None:
    with _client() as client:
        posted = client.post("/api/v1/shell/navigate", json={"route": "/performance"})
        navigate_id = posted.json()["id"]
        acked = client.post("/api/v1/shell/navigate/ack", json={"id": navigate_id})
        pending = client.get("/api/v1/shell/navigate/pending")

    assert acked.status_code == 200
    assert acked.json() == {"acknowledged": True}
    assert pending.json() == {"pending": None}


def test_ack_returns_404_on_id_mismatch() -> None:
    with _client() as client:
        client.post("/api/v1/shell/navigate", json={"route": "/performance"})
        response = client.post("/api/v1/shell/navigate/ack", json={"id": "wrong-id"})

    assert response.status_code == 404


def test_last_post_wins_pending_slot() -> None:
    with _client() as client:
        first = client.post("/api/v1/shell/navigate", json={"route": "/performance"})
        second = client.post(
            "/api/v1/shell/navigate", json={"route": "/performance/preload1"}
        )
        pending = client.get("/api/v1/shell/navigate/pending")

    assert first.json()["id"] != second.json()["id"]
    assert pending.json()["pending"]["route"] == "/performance/preload1"


@pytest.mark.parametrize(
    "route",
    [
        "",
        "performance",
        "//evil.example/performance",
        "https://evil.example/performance",
        "/library",
    ],
)
def test_disallowed_routes_return_422(route: str) -> None:
    with _client() as client:
        response = client.post("/api/v1/shell/navigate", json={"route": route})

    assert response.status_code == 422


def test_shell_navigate_then_mirror_open_integration() -> None:
    """POST navigate -> shell poll -> performance mirror registration."""
    with _client() as client:
        posted = client.post("/api/v1/shell/navigate", json={"route": "/performance"})
        navigate_id = posted.json()["id"]
        pending = client.get("/api/v1/shell/navigate/pending").json()["pending"]
        assert pending["id"] == navigate_id
        client.put(
            "/api/v1/state/ui-mirror",
            json={"client_open": True, "context_state": "running"},
        )
        acked = client.post("/api/v1/shell/navigate/ack", json={"id": navigate_id})
        mirror = client.get("/api/v1/state/ui-mirror")

    assert acked.status_code == 200
    assert mirror.status_code == 200
    assert mirror.json()["client_open"] is True
