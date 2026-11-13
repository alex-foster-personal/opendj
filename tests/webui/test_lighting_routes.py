"""LIGHT-10573: lighting stage routes registered and wired to the frame feed.

[if] POST ingest, or the daemon GET patch and WS frames behind it, break in the assembled app [then] fail, [else stop].
"""

from __future__ import annotations

import asyncio
import importlib
import json
import pkgutil
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import APIRouter, FastAPI
from starlette.testclient import TestClient

from apps.feature_flags import load_flags
from apps.lighting.contract import LIGHTING_FLAG_ID
from apps.lighting.fixtures import parse_profile
from apps.lighting.http import FRAMES_CLOCK_ATTR, FRAMES_MAX_HZ, load_store, stage_patch_body
from apps.lighting.http import router as daemon_router
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
    """Webui routes mounted before the daemon router, as app_wiring assembles them."""
    flag_dir = tmp_path / "flags"
    flag_dir.mkdir(exist_ok=True)
    (flag_dir / "feature-flags.json").write_text(json.dumps({LIGHTING_FLAG_ID: True}))
    app = FastAPI()
    app.state.feature_flags = load_flags(flag_dir)
    app.state.lighting_patch_path = tmp_path / "patch.json"
    app.state.lighting_profiles_dir = tmp_path / "profiles"
    setattr(app.state, FRAMES_CLOCK_ATTR, lambda: 0)
    app.include_router(lighting_routes.router, prefix="/api/v1")
    app.include_router(daemon_router, prefix="/api/v1")
    return app


def _valid_frame(t_ns: int = 0) -> dict:
    return {
        "t_ns": t_ns,
        "fixtures": [
            {"id": "par-1", "rgb": [255, 0, 0], "intensity": 1.0, "pan_deg": None, "tilt_deg": None, "strobe_hz": 0}
        ],
    }


def _save_patch(tmp_path: Path) -> None:
    save_store(tmp_path / "patch.json", VenueStore("club", (Patch("club", (_par("par-1", 1),)),)), PROFILES)


@pytest.mark.requirement("LIGHT-14090")
def test_assembled_frames_ws_carries_patch_version_throttled(tmp_path: Path) -> None:
    """[if] the assembled app serves ws /api/v1/lighting/frames [then] messages carry patch_version at <=30/s, [else stop]."""
    _save_patch(tmp_path)
    app = _app(tmp_path)
    paths = [getattr(r, "path", "") for r in app.routes]
    assert paths.count("/api/v1/lighting/frames") == 2  # POST ingest + daemon websocket only
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    with (
        patch.object(asyncio, "sleep", fake_sleep),
        TestClient(app) as client,
        client.websocket_connect("/api/v1/lighting/frames") as ws,
    ):
        client.post("/api/v1/lighting/frames", json=_valid_frame(1))
        first = ws.receive_json()
        client.post("/api/v1/lighting/frames", json=_valid_frame(2))
        second = ws.receive_json()
    assert first["patch_version"] == stage_patch_body(load_store(tmp_path / "patch.json"))["version"]
    assert second["patch_version"] == first["patch_version"]
    assert sleeps and min(sleeps) >= 1 / FRAMES_MAX_HZ - 1e-6
    assert FRAMES_MAX_HZ == 30


@pytest.mark.requirement("LIGHT-14090")
def test_assembled_get_patch_is_daemon_document(tmp_path: Path) -> None:
    """[if] GET /api/v1/lighting/patch is read in the assembled app [then] the daemon venue store answers, [else stop]."""
    _save_patch(tmp_path)
    with TestClient(_app(tmp_path)) as client:
        res = client.get("/api/v1/lighting/patch")
    assert res.status_code == 200
    body = res.json()
    assert body["active"] == "club"
    assert body["patches"][0]["fixtures"][0]["id"] == "par-1"


@pytest.mark.requirement("LIGHT-14090")
def test_posted_frame_reaches_connected_socket(tmp_path: Path) -> None:
    """[if] a frame is POSTed to /lighting/frames [then] a connected socket client still receives it, [else stop]."""
    frame = _valid_frame(7)
    with TestClient(_app(tmp_path)) as client, client.websocket_connect("/api/v1/lighting/frames") as ws:
        post = client.post("/api/v1/lighting/frames", json=frame)
        assert post.status_code == 202
        got = ws.receive_json()
    assert got["t_ns"] == 7
    assert got["fixtures"] == frame["fixtures"]


def test_post_frame_invalid(tmp_path: Path) -> None:
    """[if] POST missing t_ns is accepted [then] stop."""
    with TestClient(_app(tmp_path)) as client:
        res = client.post("/api/v1/lighting/frames", json={"fixtures": []})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "frame_invalid"


def test_lighting_router_auto_registered() -> None:
    """[if] lighting router is not in _routers_under [then] live mode has no endpoints."""
    routers = _routers_under(routes)
    assert any(r is lighting_routes.router for r in routers)
    names = {info.name for info in pkgutil.iter_modules(routes.__path__)}
    assert "lighting" in names
    mod = importlib.import_module("apps.webui.server.routes.lighting")
    assert isinstance(mod.router, APIRouter)
    assert mod.MOUNT_UNDER_API_PREFIX is True
