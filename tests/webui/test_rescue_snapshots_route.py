"""RESCUE-01: HTTP routes for rescue snapshots.

[if] a snapshot is POSTed [then] it lands in slot 0 and latest and index serve it, [else stop].
[if] the ring is empty [then] the latest route returns 404, [else stop].
[if] a snapshot with deck_layout "less" is POSTed [then] latest serves "less" back, [else stop] (RESCUE-08).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.webui.server.routes.rescue_snapshots import router as rescue_router
from tests.webui.rescue_snapshot_builders import minimal_rescue_snapshot

pytestmark = pytest.mark.requirement("RESCUE-01")


def _minimal_snapshot(captured_at_ms: int) -> dict:
    return minimal_rescue_snapshot(
        captured_at_ms,
        reason="periodic",
        master_deck=1,
        playing=True,
        position_ms=1000,
    )


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


@pytest.mark.asyncio
@pytest.mark.requirement("RESCUE-08")
@pytest.mark.parametrize("layout", ["less", "more"])
async def test_deck_layout_survives_the_real_ring(app: FastAPI, layout: str) -> None:
    """If the ring stores or serves a different deck_layout than was POSTed, restore flips the layout."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        post = await client.post(
            "/api/v1/performance/rescue-snapshots",
            json={**_minimal_snapshot(43_000), "deck_layout": layout},
        )
        assert post.status_code == 202, post.text
        latest = await client.get("/api/v1/performance/rescue-snapshots/latest")
        assert latest.status_code == 200
        assert latest.json()["deck_layout"] == layout
