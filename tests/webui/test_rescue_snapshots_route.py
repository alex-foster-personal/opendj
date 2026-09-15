"""RESCUE-01: HTTP routes for rescue snapshots.

[if] a snapshot is POSTed [then] it lands in slot 0 and latest and index serve it, [else stop].
[if] the ring is empty [then] the latest route returns 404, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.webui.server.routes.rescue_snapshots import router as rescue_router

pytestmark = pytest.mark.requirement("RESCUE-01")


def _minimal_snapshot(captured_at_ms: int) -> dict:
    deck = {
        "deck_id": 1,
        "stable_id": "abc",
        "source_path": None,
        "playing": True,
        "position_ms": 1000,
        "beat_stamp": {"kind": "sample", "position_ms": 1000},
        "pitch": 1.0,
        "pitch_range": 8,
        "master_tempo_enabled": True,
        "key_sync_enabled": False,
        "quantize_enabled": True,
        "beat_sync_enabled": True,
        "sync_mode": "bar",
        "is_master": True,
        "cue_ms": None,
        "loop": None,
        "hot_cue_armed": None,
        "stems": {
            "vocal": {"muted": False, "solo": False, "gain": 1.0},
            "instrumental": {"muted": False, "solo": False, "gain": 1.0},
            "drums": {"muted": False, "solo": False, "gain": 1.0},
        },
        "mixer_channel": {
            "trim": 0.5,
            "eq_high": 0.5,
            "eq_mid": 0.5,
            "eq_low": 0.5,
            "filter": 0.5,
            "fader": 1.0,
            "assign": "THRU",
            "cue_enabled": False,
        },
    }
    return {
        "schema": 1,
        "captured_at_ms": captured_at_ms,
        "reason": "periodic",
        "app_posture": "gig",
        "master_deck": 1,
        "playlist_id": None,
        "decks": {str(i): {**deck, "deck_id": i} for i in range(1, 5)},
        "mixer": {
            "crossfader": 0.5,
            "master": 1.0,
            "headphones": {
                "mix": 0.5,
                "level": 0.5,
                "output_mode": "practice",
                "selected_master_output_device_id": None,
                "selected_output_device_id": None,
            },
        },
    }


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    application = FastAPI()
    application.state.data_dir = tmp_path
    application.include_router(rescue_router, prefix="/api/v1")
    return application


@pytest.mark.asyncio
async def test_post_and_get_latest(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        post = await client.post(
            "/api/v1/performance/rescue-snapshots",
            json=_minimal_snapshot(42_000),
        )
        assert post.status_code == 202
        body = post.json()
        assert body["slot"] == 0
        latest = await client.get("/api/v1/performance/rescue-snapshots/latest")
        assert latest.status_code == 200
        assert latest.json()["captured_at_ms"] == 42_000
        index = await client.get("/api/v1/performance/rescue-snapshots/index")
        assert index.status_code == 200
        assert index.json()["newest_slot"] == 0


@pytest.mark.asyncio
async def test_latest_404_when_empty(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        latest = await client.get("/api/v1/performance/rescue-snapshots/latest")
        assert latest.status_code == 404
