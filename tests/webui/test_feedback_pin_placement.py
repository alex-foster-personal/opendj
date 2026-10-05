"""Pin placement against the UI (feedback_pin_placement.py, Thu 1 Oct 2026).

A pin may carry ``element_offset`` (click inside its ``anchor`` element's box)
and up to three ``nearby_anchors`` so the browser can keep it in place when
that element is lost. Both are optional and additive.

Regression lines:
- if a pin posted with placement does not return it, or does not persist it
  to comments.json, the browser cannot re-place it after a reload
- if a pin posted WITHOUT placement (every older client and agent) is refused
  or returns a different shape for the old fields, old clients broke
- if a stored pin from before placement existed stops listing, old pins vanish
- if a fourth nearby anchor, an offset outside 0..100, or an offset with no
  anchor is accepted, the stored record cannot be resolved honestly
- if a follow-on does not inherit its parent's placement, it floats at the
  parent's old screen position while the parent stays on its element
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend

_PLACEMENT = {
    "element_offset": {"dx_pct": 25.0, "dy_pct": 50.0},
    "nearby_anchors": [
        {"selector": '[data-testid="mixer"]', "dx_px": -20.0, "dy_px": 30.5},
        {"selector": "#topbar", "dx_px": 400.0, "dy_px": 400.0},
    ],
}


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


def _body(**extra: object) -> dict[str, object]:
    return {
        "x_pct": 41.5,
        "y_pct": 12.0,
        "anchor": "#deck-a",
        "page": "/performance",
        "text": "keep me on the deck",
        "ui": "chrome-loop",
        "viewport_width": 1280,
        "viewport_height": 800,
        **extra,
    }


def _stored(client: TestClient) -> list[dict[str, object]]:
    path = client.data_dir / "feedback" / "comments.json"  # type: ignore[attr-defined]
    return json.loads(path.read_text())["comments"]


def test_placement_roundtrips_and_persists(fb: TestClient) -> None:
    r = fb.post("/api/v1/feedback/comments", json=_body(**_PLACEMENT))
    assert r.status_code == 201, r.text
    pin = r.json()
    assert pin["element_offset"] == _PLACEMENT["element_offset"]
    assert pin["nearby_anchors"] == _PLACEMENT["nearby_anchors"]
    (stored,) = _stored(fb)
    assert stored["element_offset"] == _PLACEMENT["element_offset"]
    assert stored["nearby_anchors"] == _PLACEMENT["nearby_anchors"]
    (listed,) = fb.get("/api/v1/feedback/comments").json()["comments"]
    assert listed["nearby_anchors"] == _PLACEMENT["nearby_anchors"]


def test_pin_without_placement_still_creates_with_empty_placement(fb: TestClient) -> None:
    r = fb.post("/api/v1/feedback/comments", json=_body())
    assert r.status_code == 201, r.text
    pin = r.json()
    assert pin["element_offset"] is None
    assert pin["nearby_anchors"] == []
    assert (pin["x_pct"], pin["y_pct"], pin["anchor"]) == (41.5, 12.0, "#deck-a")


def test_a_stored_pin_from_before_placement_still_lists(fb: TestClient) -> None:
    fb.post("/api/v1/feedback/comments", json=_body())
    path = fb.data_dir / "feedback" / "comments.json"  # type: ignore[attr-defined]
    doc = json.loads(path.read_text())
    for c in doc["comments"]:
        c.pop("element_offset")
        c.pop("nearby_anchors")
    path.write_text(json.dumps(doc))
    (listed,) = fb.get("/api/v1/feedback/comments").json()["comments"]
    assert listed["element_offset"] is None
    assert listed["nearby_anchors"] == []
    assert (listed["x_pct"], listed["y_pct"]) == (41.5, 12.0)


@pytest.mark.parametrize(
    "extra",
    [
        {"nearby_anchors": [{"selector": f"#a{i}", "dx_px": 0, "dy_px": 0} for i in range(4)]},
        {"element_offset": {"dx_pct": 120, "dy_pct": 5}},
        {"element_offset": {"dx_pct": 10, "dy_pct": 5}, "anchor": None},
        {"nearby_anchors": [{"selector": "", "dx_px": 0, "dy_px": 0}]},
    ],
    ids=["four-anchors", "offset-out-of-box", "offset-without-anchor", "empty-selector"],
)
def test_unresolvable_placement_is_refused(fb: TestClient, extra: dict[str, object]) -> None:
    r = fb.post("/api/v1/feedback/comments", json=_body(**extra))
    assert r.status_code == 422, r.text
    assert not (fb.data_dir / "feedback" / "comments.json").exists() or _stored(fb) == []  # type: ignore[attr-defined]


def test_follow_on_inherits_parent_placement(fb: TestClient) -> None:
    parent = fb.post("/api/v1/feedback/comments", json=_body(**_PLACEMENT)).json()
    fb.patch(f"/api/v1/feedback/comments/{parent['id']}", json={"status": "fixed"})
    child = fb.post(f"/api/v1/feedback/comments/{parent['id']}/follow-on", json={})
    assert child.status_code == 201, child.text
    assert child.json()["element_offset"] == _PLACEMENT["element_offset"]
    assert child.json()["nearby_anchors"] == _PLACEMENT["nearby_anchors"]
