"""NOWP-11489: GET /api/v1/now-playing (issue #10336).

[if] the route or mirror ingest disagrees with settle and stale rules [then] fail, [else stop].
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.telemetry import MIRROR_RECEIVED_MONOTONIC_KEY
from apps.webui.server.routes import now_playing, state

pytestmark = pytest.mark.requirement("NOWP-11489")

_CLOCK = {"now": 0.0}


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(state.router, prefix="/api/v1")
    app.include_router(now_playing.router, prefix="/api/v1")
    return TestClient(app)


def _heard_mirror(stable_id: str = "sid-1", gain: float = 1.0) -> dict[str, Any]:
    return {
        "client_open": True,
        "decks": {
            "1": {
                "stable_id": stable_id,
                "title": "Track One",
                "artist": "Artist One",
                "key": "Am",
                "bpm": 124.0,
                "playing": True,
                "position": {"ms": 0},
            }
        },
        "mixer": {"crossfader": 0.5, "channels": {"1": {"fader": 1.0, "assign": "THRU", "on_air_gain": gain}}},
        "room_output": {"speakers_on": True, "room_path": "room_only"},
    }


def test_never_published() -> None:
    with _client() as c:
        body = c.get("/api/v1/now-playing").json()
    assert body == {"track": None, "previous": [], "stale": True, "generation": 0}


def test_put_then_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter([0.0, 1.0, 2.0, 3.0])

    def mono() -> float:
        return next(times, 3.0)

    monkeypatch.setattr(state, "_received_monotonic_s", mono)
    monkeypatch.setattr(now_playing, "_now_monotonic", lambda: 3.0)
    with _client() as c:
        for _ in range(4):
            assert c.put("/api/v1/state/ui-mirror", json=_heard_mirror()).status_code == 202
        body = c.get("/api/v1/now-playing").json()
    assert body["stale"] is False
    assert body["generation"] == 1
    assert body["track"]["stable_id"] == "sid-1"
    assert body["track"]["key_camelot"] is not None
    assert body["track"]["key_musical"] is not None


def test_stale_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "_received_monotonic_s", lambda: 0.0)
    clock = {"t": 0.0}
    monkeypatch.setattr(now_playing, "_now_monotonic", lambda: clock["t"])
    with _client() as c:
        c.put("/api/v1/state/ui-mirror", json=_heard_mirror())
        for t in (1.0, 2.0, 3.0):
            monkeypatch.setattr(state, "_received_monotonic_s", lambda t=t: t)
            c.put("/api/v1/state/ui-mirror", json=_heard_mirror())
        clock["t"] = 6.5
        stale = c.get("/api/v1/now-playing").json()
        assert stale["stale"] is True
        assert stale["track"]["stable_id"] == "sid-1"
        c.delete("/api/v1/state/ui-mirror")
        after_delete = c.get("/api/v1/now-playing").json()
    assert after_delete["stale"] is True
    assert after_delete["track"]["stable_id"] == "sid-1"


def test_switch_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    seq = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
    monkeypatch.setattr(state, "_received_monotonic_s", lambda: next(seq))
    monkeypatch.setattr(now_playing, "_now_monotonic", lambda: 7.0)
    with _client() as c:
        for _ in range(4):
            c.put("/api/v1/state/ui-mirror", json=_heard_mirror("sid-1"))
        for _ in range(4):
            c.put("/api/v1/state/ui-mirror", json=_heard_mirror("sid-2"))
        body = c.get("/api/v1/now-playing").json()
    assert body["track"]["stable_id"] == "sid-2"
    assert body["previous"][0]["stable_id"] == "sid-1"


def test_answer_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter([0.0, 1.0, 2.0, 3.0])
    monkeypatch.setattr(state, "_received_monotonic_s", lambda: next(times))
    monkeypatch.setattr(now_playing, "_now_monotonic", lambda: 3.0)
    with _client() as c:
        for _ in range(4):
            c.put("/api/v1/state/ui-mirror", json=_heard_mirror())
        body = c.get("/api/v1/now-playing").json()
    assert set(body) == {"track", "previous", "stale", "generation"}
    assert set(body["track"]) == {
        "stable_id",
        "artist",
        "title",
        "key_camelot",
        "key_musical",
        "bpm",
        "deck",
        "since",
    }


def test_router_discovery() -> None:
    import importlib
    import pkgutil

    from apps.webui.server import routes as routes_package

    names = {
        info.name
        for info in sorted(pkgutil.iter_modules(routes_package.__path__), key=lambda i: i.name)
        if getattr(importlib.import_module(f"{routes_package.__name__}.{info.name}"), "MOUNT_UNDER_API_PREFIX", False)
    }
    assert "now_playing" in names


def test_mirror_stamps_monotonic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "_received_monotonic_s", lambda: 1.5)
    with _client() as c:
        c.put("/api/v1/state/ui-mirror", json=_heard_mirror())
        stored = c.app.state.ui_mirror
    assert isinstance(stored, dict)
    assert stored[MIRROR_RECEIVED_MONOTONIC_KEY] == 1.5
