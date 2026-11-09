"""LIGHT-10573: lighting stage routes registered and wired to the frame feed.

[if] GET/POST/WS /api/v1/lighting/* do not match the stage visualiser contract [then] fail, [else stop].
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from starlette.testclient import TestClient

from apps.lighting.fixtures import parse_profile
from apps.lighting.patch import Patch, PatchedFixture, StagePosition, VenueStore, save_store
from apps.webui.server import routes
from apps.webui.server.app_wiring import _routers_under
from apps.webui.server.routes import lighting as lighting_routes

pytestmark = [pytest.mark.requirement("LIGHT-10573")]

PAR = parse_profile(
    {
        "id": "test-rgb-par-3",
        "name": "Test RGB Par",
        "kind": "par",
        "modes": [{"name": "3ch", "channels": [{"capability": c} for c in ("red", "green", "blue")]}],
    }
)
PROFILES = {PAR.id: PAR}


def _par(fixture_id: str, address: int) -> PatchedFixture:
    return PatchedFixture(fixture_id, PAR.id, "3ch", 1, address, ("pars",), StagePosition(0.25, 0.5))


def _app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(lighting_routes.router, prefix="/api/v1")
    app.state.lighting_patch_path = tmp_path / "patch.json"
    return app


def _valid_frame() -> dict:
    return {
        "t_ns": 0,
        "patch_version": "abc",
        "fixtures": [
            {"id": "par-1", "rgb": [255, 0, 0], "intensity": 1.0, "pan_deg": None, "tilt_deg": None, "strobe_hz": 0}
        ],
    }


def test_get_patch_no_file_is_no_patch(tmp_path: Path) -> None:
    """[if] GET without a patch file does not return no_patch [then] stop."""
    with TestClient(_app(tmp_path)) as client:
        res = client.get("/api/v1/lighting/patch")
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "no_patch"


def test_get_patch_valid_store(tmp_path: Path) -> None:
    """[if] GET does not return active fixture layout [then] stop."""
    path = tmp_path / "patch.json"
    store = VenueStore("club", (Patch("club", (_par("par-1", 1),)),))
    save_store(path, store, PROFILES)
    with TestClient(_app(tmp_path)) as client:
        res = client.get("/api/v1/lighting/patch")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "club"
    assert body["fixtures"][0]["id"] == "par-1"
    assert body["fixtures"][0]["x"] == 0.25


def test_get_patch_corrupt_is_patch_invalid(tmp_path: Path) -> None:
    """[if] corrupt patch file is not patch_invalid [then] stop."""
    path = tmp_path / "patch.json"
    path.write_text("{not json", encoding="utf-8")
    with TestClient(_app(tmp_path)) as client:
        res = client.get("/api/v1/lighting/patch")
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "patch_invalid"


def test_post_frame_invalid(tmp_path: Path) -> None:
    """[if] POST missing t_ns is accepted [then] stop."""
    with TestClient(_app(tmp_path)) as client:
        res = client.post("/api/v1/lighting/frames", json={"fixtures": []})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "frame_invalid"


def test_post_frame_valid(tmp_path: Path) -> None:
    """[if] POST valid frame does not return seq [then] stop."""
    with TestClient(_app(tmp_path)) as client:
        res = client.post("/api/v1/lighting/frames", json=_valid_frame())
    assert res.status_code == 202
    assert res.json()["seq"] == 1


def test_websocket_receives_posted_frame(tmp_path: Path) -> None:
    """[if] websocket misses POSTed frame [then] stop."""
    frame = _valid_frame()
    with TestClient(_app(tmp_path)) as client, client.websocket_connect("/api/v1/lighting/frames") as ws:
        post = client.post("/api/v1/lighting/frames", json=frame)
        assert post.status_code == 202
        got = ws.receive_json()
    assert got == frame


def test_websocket_join_after_publish_gets_latest(tmp_path: Path) -> None:
    """[if] websocket opened after publish misses latest [then] stop."""
    frame = _valid_frame()
    with TestClient(_app(tmp_path)) as client:
        post = client.post("/api/v1/lighting/frames", json=frame)
        assert post.status_code == 202
        with client.websocket_connect("/api/v1/lighting/frames") as ws:
            got = ws.receive_json()
    assert got == frame


def test_lighting_router_auto_registered() -> None:
    """[if] lighting router is not in _routers_under [then] live mode has no endpoints."""
    routers = _routers_under(routes)
    assert any(r is lighting_routes.router for r in routers)
    names = {info.name for info in pkgutil.iter_modules(routes.__path__)}
    assert "lighting" in names
    mod = importlib.import_module("apps.webui.server.routes.lighting")
    assert isinstance(mod.router, APIRouter)
    assert mod.MOUNT_UNDER_API_PREFIX is True
