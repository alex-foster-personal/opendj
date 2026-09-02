"""PATCH /api/v1/feedback/comments/{id}: the agent-native half of the pin lifecycle (#858).

[if] a fix agent PATCHes status/issue_url/agent_note [then] the pin keeps its text and
    position and gains those fields plus updated_at
[if] the id is unknown [then] 404 COMMENT_NOT_FOUND, nothing written
[if] the body carries no field [then] 422 NO_CHANGES
[if] status is not one of the lifecycle states [then] 422
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    # Mirrors tests/webui/test_feedback_routes.py: the feedback dir is resolved from
    # app.state.data_dir, never from the environment.
    from apps.webui.server.app import create_app

    app = create_app(mount_frontend=False)
    app.state.data_dir = tmp_path
    return TestClient(app)


def _create(client: TestClient) -> dict:
    r = client.post(
        "/api/v1/feedback/comments",
        json={"x_pct": 10, "y_pct": 20, "anchor": ".bank", "page": "/performance", "text": "hot cues ignore BSM"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_patch_records_lifecycle_fields_without_touching_the_pin(client: TestClient, tmp_path: Path) -> None:
    pin = _create(client)
    r = client.patch(
        f"/api/v1/feedback/comments/{pin['id']}",
        json={"status": "issued", "issue_url": "https://github.com/x/y/issues/1", "agent_note": "queued as #1"},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["text"] == "hot cues ignore BSM" and out["x_pct"] == 10 and out["anchor"] == ".bank"
    assert out["status"] == "issued" and out["issue_url"].endswith("/issues/1") and out["agent_note"] == "queued as #1"
    assert out["updated_at"] is not None, "if a patch leaves no updated_at then nobody can tell when the agent acted - broken"
    on_disk = json.loads((tmp_path / "feedback" / "comments.json").read_text())["comments"]
    assert on_disk[0]["status"] == "issued", "if the patch is not persisted then the pin forgets on reload - broken"
    listed = client.get("/api/v1/feedback/comments").json()["comments"]
    assert listed[0]["agent_note"] == "queued as #1"


def test_patch_unknown_id_is_404_and_writes_nothing(client: TestClient, tmp_path: Path) -> None:
    _create(client)
    before = (tmp_path / "feedback" / "comments.json").read_text()
    r = client.patch("/api/v1/feedback/comments/nope", json={"status": "fixed"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "COMMENT_NOT_FOUND"
    assert (tmp_path / "feedback" / "comments.json").read_text() == before


def test_patch_rejects_empty_body_and_unknown_status(client: TestClient) -> None:
    pin = _create(client)
    assert client.patch(f"/api/v1/feedback/comments/{pin['id']}", json={}).status_code == 422
    assert client.patch(f"/api/v1/feedback/comments/{pin['id']}", json={"status": "done"}).status_code == 422
