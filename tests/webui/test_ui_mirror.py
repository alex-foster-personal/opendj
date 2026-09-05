"""AGENT-02 UI-mirror HTTP contract.

[if] a client has not opened the performance page [then] GET returns 409 with
     client_open false, never an empty document.
[if] a client pushes the screen mirror [then] GET returns that exact document.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.routes.state import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_ui_mirror_refuses_an_empty_document_when_no_page_is_open() -> None:
    with _client() as client:
        response = client.get("/api/v1/state/ui-mirror")

    assert response.status_code == 409
    assert response.json() == {"client_open": False}


def test_ui_mirror_returns_the_latest_document_pushed_by_the_open_page() -> None:
    mirror = {
        "decks": {"1": {"playing": True, "audible": False}},
        "toasts": [{"id": "silent-while-playing"}],
        "controls": {"grid-adjust": "inert"},
    }
    with _client() as client:
        published = client.put("/api/v1/state/ui-mirror", json=mirror)
        response = client.get("/api/v1/state/ui-mirror")

    assert published.status_code == 202
    assert response.status_code == 200
    assert response.json() == mirror
