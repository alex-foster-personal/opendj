"""Acceptance tests for apps.opendj_pins mountable comment-pins router (#11294)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from apps.opendj_pins.http import mount_pins_router
from apps.opendj_pins.router import mark_anchor_moved, mark_pin_harvested
from apps.webui.server.app import create_app


def _pins_app(tmp_path: Path) -> TestClient:
    app = FastAPI()
    mount_pins_router(app, sqlite_path=tmp_path / "pins.sqlite")
    return TestClient(app)


def _pin_body(**overrides: object) -> dict:
    body = {
        "x_pct": 12.5,
        "y_pct": 40.0,
        "anchor": "button.save",
        "page": "/preview",
        "text": "save button misaligned",
        "ui": "chrome-loop",
        "viewport_width": 1280,
        "viewport_height": 720,
    }
    body.update(overrides)
    return body


def _openapi_comment_fields() -> set[str]:
    schema = json.loads(Path("apps/webui/openapi.json").read_text(encoding="utf-8"))
    return set(schema["components"]["schemas"]["CommentOut"]["properties"])


@pytest.mark.requirement("PINS-12172")
def test_mount_empty_list_returns_200(tmp_path: Path) -> None:
    """[if] router mounts with temp SQLite [then] GET /feedback/comments is 200 and empty, [else stop]."""
    client = _pins_app(tmp_path)
    response = client.get("/feedback/comments")
    assert response.status_code == 200
    assert response.json() == {"comments": []}


@pytest.mark.requirement("PINS-12172")
def test_post_then_get_matches_openapi_fields(tmp_path: Path) -> None:
    """[if] POST creates a pin [then] GET lists it with OpenAPI CommentOut fields, [else stop]."""
    client = _pins_app(tmp_path)
    created = client.post("/feedback/comments", json=_pin_body()).json()
    assert created["status"] == "open"
    assert created["text"] == "save button misaligned"
    listed = client.get("/feedback/comments").json()["comments"]
    assert len(listed) == 1
    assert set(listed[0]) >= _openapi_comment_fields() & set(created)
    assert listed[0]["id"] == created["id"]


@pytest.mark.requirement("PINS-12172")
def test_reply_attachment_and_resolve_reflect_on_get(tmp_path: Path) -> None:
    """[if] reply, attachment, resolve are posted [then] next GET shows each on the pin, [else stop]."""
    client = _pins_app(tmp_path)
    pin = client.post("/feedback/comments", json=_pin_body()).json()
    pin_id = pin["id"]
    replied = client.post(
        f"/feedback/comments/{pin_id}/replies",
        json={"text": "still broken after reload", "author": "operator"},
    )
    assert replied.status_code == 200
    image = Image.new("RGB", (4, 4), color="red")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    attached = client.post(
        f"/feedback/comments/{pin_id}/attachment",
        files={"file": ("shot.png", buf.getvalue(), "image/png")},
    )
    assert attached.status_code == 201
    resolved = client.patch(f"/feedback/comments/{pin_id}", json={"status": "fixed"})
    assert resolved.status_code == 200
    body = client.get("/feedback/comments").json()["comments"][0]
    assert any(reply["text"] == "still broken after reload" for reply in body["replies"])
    assert body["attachment"]["content_type"] == "image/png"
    assert body["status"] == "fixed"


@pytest.mark.requirement("PINS-12172")
def test_harvested_and_anchor_moved_round_trip_sqlite(tmp_path: Path) -> None:
    """[if] pin is harvested or anchor-moved in store [then] GET returns those states, [else stop]."""
    client = _pins_app(tmp_path)
    pin = client.post("/feedback/comments", json=_pin_body()).json()
    store = client.app.state.opendj_pins_store  # type: ignore[attr-defined]
    mark_pin_harvested(store, pin["id"])
    harvested = client.get("/feedback/comments", params={"state": "harvested"}).json()["comments"]
    assert harvested[0]["status"] == "harvested"
    assert harvested[0]["harvested_at"]
    mark_anchor_moved(store, pin["id"], "button.save-moved")
    moved = client.get("/feedback/comments").json()["comments"][0]
    assert moved["anchor"] == "button.save-moved"


@pytest.mark.requirement("PINS-12172")
def test_engine_feedback_imports_still_run(fb_tmp_path: Path) -> None:
    """[if] engine tests import apps.webui.server.routes.feedback [then] todo create still passes, [else stop]."""
    from apps.webui.server.backend import InMemoryBackend

    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
    )
    app.state.data_dir = fb_tmp_path
    client = TestClient(app)
    response = client.post("/api/v1/feedback/todos", json={"title": "check pins package split"})
    assert response.status_code == 201


@pytest.fixture
def fb_tmp_path(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    return data
