"""AGENT-02 UI-mirror HTTP contract.

[if] a client has not opened the performance page [then] GET returns 409 with
     client_open false, never an empty document.
[if] a client pushes the screen mirror [then] GET returns that document with the
     same pushed fields plus a server-stamped received_at on ingest.
"""
from __future__ import annotations

import re
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.routes.state import router

_RECEIVED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _assert_received_at(value: str) -> None:
    assert _RECEIVED_AT_RE.match(value), f"received_at must be UTC ISO ms: {value}"


def test_ui_mirror_refuses_an_empty_document_when_no_page_is_open() -> None:
    with _client() as client:
        response = client.get("/api/v1/state/ui-mirror")

    assert response.status_code == 409
    body = response.json()
    assert body == {"client_open": False}
    assert "received_at" not in body


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
    body = response.json()
    for key, value in mirror.items():
        assert body[key] == value
    _assert_received_at(body["received_at"])


def test_ui_mirror_received_at_is_stamped_on_ingest_not_on_get() -> None:
    mirror = {"client_open": True, "decks": {}}
    with _client() as client:
        client.put("/api/v1/state/ui-mirror", json=mirror)
        first = client.get("/api/v1/state/ui-mirror").json()["received_at"]
        second = client.get("/api/v1/state/ui-mirror").json()["received_at"]

    assert first == second


def test_ui_mirror_received_at_moves_forward_on_each_put() -> None:
    mirror = {"client_open": True, "decks": {}}
    with _client() as client:
        client.put("/api/v1/state/ui-mirror", json=mirror)
        t0 = client.get("/api/v1/state/ui-mirror").json()["received_at"]
        time.sleep(0.02)
        client.put("/api/v1/state/ui-mirror", json=mirror)
        t1 = client.get("/api/v1/state/ui-mirror").json()["received_at"]

    assert t1 > t0


def test_ui_mirror_received_at_cannot_be_spoofed_by_the_client() -> None:
    spoofed = {
        "client_open": True,
        "received_at": "1999-01-01T00:00:00.000Z",
    }
    with _client() as client:
        client.put("/api/v1/state/ui-mirror", json=spoofed)
        received_at = client.get("/api/v1/state/ui-mirror").json()["received_at"]

    assert received_at != "1999-01-01T00:00:00.000Z"
    _assert_received_at(received_at)


def test_ui_mirror_published_at_round_trips_from_the_page() -> None:
    mirror = {
        "client_open": True,
        "published_at": "2026-09-11T18:50:03.000Z",
        "decks": {},
    }
    with _client() as client:
        client.put("/api/v1/state/ui-mirror", json=mirror)
        body = client.get("/api/v1/state/ui-mirror").json()

    assert body["published_at"] == "2026-09-11T18:50:03.000Z"
    _assert_received_at(body["received_at"])
    assert body["received_at"] != body["published_at"]
