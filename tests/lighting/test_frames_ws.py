"""Frames websocket for the visualiser (issue #9897)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apps.feature_flags import load_flags
from apps.lighting.contract import LIGHTING_FLAG_ID
from apps.lighting.http import (
    FRAMES_CLOCK_ATTR,
    PATCH_PATH_ATTR,
    PROFILES_DIR_ATTR,
    FrameThrottle,
    frame_feed,
    router,
)


def _app(tmp_path: Path, *, flag_on: bool) -> FastAPI:
    flag_dir = tmp_path / "flags"
    flag_dir.mkdir()
    if flag_on:
        (flag_dir / "feature-flags.json").write_text(json.dumps({LIGHTING_FLAG_ID: True}))
    app = FastAPI()
    app.state.feature_flags = load_flags(flag_dir)
    setattr(app.state, PATCH_PATH_ATTR, tmp_path / "patch.json")
    setattr(app.state, PROFILES_DIR_ATTR, tmp_path / "profiles")
    ticks: Iterator[int] = iter(range(0, 10**15, 10**9))
    setattr(app.state, FRAMES_CLOCK_ATTR, lambda: next(ticks))
    app.include_router(router, prefix="/api/v1")
    return app


def _store(*addresses: int) -> dict[str, Any]:
    fixtures = [
        {
            "id": f"par-{a}",
            "profile": "generic-rgb",
            "mode": "3ch",
            "universe": 1,
            "address": a,
            "groups": ["left"],
            "position": {"x": 0.5, "y": 0.5},
        }
        for a in addresses
    ]
    return {"version": 1, "active": "club", "patches": [{"name": "club", "fixtures": fixtures}]}


def _frame(t_ns: int) -> dict[str, Any]:
    return {
        "t_ns": t_ns,
        "fixtures": [
            {"id": "par-1", "rgb": [255, 0, 0], "intensity": 1.0, "pan_deg": 0, "tilt_deg": 0, "strobe_hz": 0}
        ],
    }


@pytest.mark.requirement("LIGHT-14050")
def test_44_fps_compositor_yields_at_most_30_per_second() -> None:
    """[if] the compositor produces 44 frames per second [then] a client receives at most 30 per second, [else stop]."""
    throttle = FrameThrottle()
    sent = [t for t in (i * 1_000_000_000 // 44 for i in range(44 * 3)) if throttle.admit(t)]
    for start in range(0, 3_000_000_000, 100_000_000):
        assert sum(start <= t < start + 1_000_000_000 for t in sent) <= 30
    assert len(sent) >= 60


@pytest.mark.requirement("LIGHT-14050")
def test_patch_edit_changes_next_patch_version(tmp_path: Path) -> None:
    """[if] the patch is edited [then] the next message carries a new patch_version, [else stop]."""
    app = _app(tmp_path, flag_on=True)
    with TestClient(app) as client:
        assert client.put("/api/v1/lighting/patch", json=_store(1)).status_code == 200
        with client.websocket_connect("/api/v1/lighting/frames") as ws:
            feed = frame_feed(app)
            ws.portal.call(feed.publish, _frame(1))
            first = ws.receive_json()
            assert client.put("/api/v1/lighting/patch", json=_store(1, 10)).status_code == 200
            ws.portal.call(feed.publish, _frame(2))
            second = ws.receive_json()
    assert first["fixtures"][0]["id"] == "par-1"
    assert first["patch_version"] and second["patch_version"]
    assert first["patch_version"] != second["patch_version"]


@pytest.mark.requirement("LIGHT-14050")
def test_flag_off_refuses_socket_with_flag_id(tmp_path: Path) -> None:
    """[if] the flag is off [then] the socket is refused with the flag id in the close reason, [else stop]."""
    with TestClient(_app(tmp_path, flag_on=False)) as client:
        assert client.get("/api/v1/lighting/frame").status_code == 409
        with client.websocket_connect("/api/v1/lighting/frames") as ws:
            assert ws.receive_json()["status"] == 409
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
    assert LIGHTING_FLAG_ID in exc.value.reason
