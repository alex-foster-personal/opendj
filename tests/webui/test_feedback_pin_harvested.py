"""Bulk harvest keeps pins on the live board (FB-15, issue #3782)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend


@pytest.fixture
def fb(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
    )
    app.state.data_dir = data_dir
    with TestClient(app) as client:
        client.data_dir = data_dir  # type: ignore[attr-defined]
        yield client


def _pin(client: TestClient, text: str = "pin") -> dict:
    r = client.post(
        "/api/v1/feedback/comments",
        json={
            "x_pct": 10,
            "y_pct": 20,
            "page": "/performance",
            "text": text,
            "ui": "chrome-loop",
            "viewport_width": 1280,
            "viewport_height": 800,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.requirement("FB-15")
def test_bulk_harvest_marks_harvested_not_cleared(fb: TestClient) -> None:
    """[if] bulk archive runs [then] pins stay open on the live board, [else stop]."""
    pin = _pin(fb)
    r = fb.post("/api/v1/feedback/archive")
    assert r.status_code == 200, r.text
    assert r.json()["comments_archived"] == 1

    live = fb.get("/api/v1/feedback/comments").json()["comments"]
    assert len(live) == 1
    assert live[0]["id"] == pin["id"]
    assert live[0]["status"] == pin["status"]
    assert live[0]["agent_snapshot_at"]


@pytest.mark.requirement("FB-15")
def test_second_harvest_is_idempotent(fb: TestClient) -> None:
    """[if] harvest runs twice [then] no duplicate archive entries, [else stop]."""
    _pin(fb)
    first = fb.post("/api/v1/feedback/archive").json()
    assert first["archived_to"] is not None

    second = fb.post("/api/v1/feedback/archive").json()
    assert second["archived_to"] is None
    assert second["comments_archived"] == 0

    archives = sorted((fb.data_dir / "feedback").glob("archive-*.json"))  # type: ignore[attr-defined]
    assert len(archives) == 1


@pytest.mark.requirement("FB-15")
def test_state_harvested_filter(fb: TestClient) -> None:
    """[if] state=harvested [then] only harvested pins return, [else stop]."""
    pin = _pin(fb, "one")
    comments_path = fb.data_dir / "feedback" / "comments.json"  # type: ignore[attr-defined]
    payload = json.loads(comments_path.read_text(encoding="utf-8"))
    payload["comments"][0]["status"] = "harvested"
    comments_path.write_text(json.dumps(payload), encoding="utf-8")

    harvested = fb.get("/api/v1/feedback/comments", params={"state": "harvested"}).json()
    assert len(harvested["comments"]) == 1
    assert harvested["comments"][0]["status"] == "harvested"
