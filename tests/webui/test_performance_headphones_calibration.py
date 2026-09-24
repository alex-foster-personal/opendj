"""CUEOUT-14 calibration over HTTP: GET/POST /performance/headphones carry the
page's calibration read model whole, including the live stage-one probe and the
post-apply verification residual, so an agent sees what the modal shows."""

from __future__ import annotations

import asyncio
from copy import deepcopy

from httpx import ASGITransport, AsyncClient

from tests.webui.test_performance_headphones import (
    _DEFAULT_HEADPHONES,
    _app,
    _fake_page,
    _open_page,
)


def test_calibrate_mirrors_the_page_calibration_state() -> None:
    """CUEOUT-14: GET /headphones carries alignment_mode, master_delay_ms and
    calibration, and a headless calibrate returns the applied state."""

    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            await _open_page(client)
            before = await client.get("/api/v1/performance/headphones")
            fake = asyncio.create_task(_fake_page(client))
            response = await client.post("/api/v1/performance/headphones/calibrate")
            command = await fake
        assert before.status_code == 200
        assert before.json()["alignment_mode"] == "hybrid"
        assert before.json()["master_delay_ms"] == 0
        assert before.json()["calibration"] == _DEFAULT_HEADPHONES["calibration"]
        assert command == {"type": "headphone_calibrate"}
        assert response.status_code == 200
        assert response.json()["calibration"]["step"] == "applied"
        assert response.json()["calibration"]["offset_ms"] == 700
        assert response.json()["calibration"]["verify_residual_ms"] == 1.5, (
            "POST /headphones/calibrate dropped verify_residual_ms: an HTTP agent cannot "
            "tell whether the applied plan verified"
        )
        assert response.json()["master_delay_ms"] == 700

    asyncio.run(run())


def test_headphones_carries_the_live_stage_one_probe() -> None:
    """CUEOUT-14: the ear-cup step is interactive, so GET /headphones must carry the
    same live probe the modal's level bar draws, or an HTTP agent drives it blind."""
    probe = {
        "bus": "cue",
        "gain": 0.5,
        "peak": 0.12,
        "lag_ms": 41.0,
        "best": 0.12,
        "threshold": 0.2,
    }
    headphones = deepcopy(_DEFAULT_HEADPHONES)
    headphones["calibration"] = {
        **headphones["calibration"],
        "step": "mic_check_cue",
        "probe": probe,
    }

    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            await _open_page(client, headphones=headphones)
            response = await client.get("/api/v1/performance/headphones")
        assert response.status_code == 200
        assert response.json()["calibration"]["probe"] == probe, (
            "GET /headphones dropped calibration.probe: the ear-cup feedback is UI-only"
        )

    asyncio.run(run())
