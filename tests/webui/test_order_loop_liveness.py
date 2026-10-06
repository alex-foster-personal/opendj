"""AGENT-22: an agent can tell, from the mirror, that the page's order loop is dead.

A dead order loop silently disables every agent command on its page while the
mirror keeps publishing, so liveness is observed by the ENGINE where the polls
land, never reported by the page.

[if] the page holds a claim open [then] ``order_loop_state`` is ``polling``, [else stop].
[if] a page has claimed an order and not answered it (a long ramp) [then] it is
    ``running_order``, not stale, [else stop].
[if] no claim is held, nothing runs and the last poll is older than
    ORDER_LOOP_STALE_MS [then] it is ``stale``, [else stop].
[if] the page never polled [then] ``never_polled`` with a null timestamp, [else stop].
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.opendj_cli.__main__ import _state_text
from apps.webui.server.routes import commands
from apps.webui.server.routes.commands import router
from apps.webui.server.routes.state import router as state_router
from tests.opendj_cli.conftest import blank_mirror

pytestmark = pytest.mark.requirement("AGENT-22")

PLAY = {"single": {"type": "play", "deck": 1, "playing": True}}


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(state_router, prefix="/api/v1")
    app.include_router(router, prefix="/api/v1")
    return app


async def _mirror(client: AsyncClient) -> dict:
    response = await client.get("/api/v1/state/ui-mirror")
    assert response.status_code == 200
    return response.json()


def test_the_mirror_shows_the_order_loop_polling_running_and_stale() -> None:
    async def run() -> None:
        app = _app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.put("/api/v1/state/ui-mirror", json={"client_open": True})).status_code == 202
            fresh = await _mirror(client)
            assert (fresh["last_order_poll_at"], fresh["order_loop_state"]) == (None, "never_polled")

            held = asyncio.create_task(client.get("/api/v1/commands/next", params={"wait_ms": 2000}))
            for _ in range(100):
                await asyncio.sleep(0.01)
                if commands.order_poll_liveness(_Req(app)).waiting:
                    break
            polling = await _mirror(client)
            assert polling["order_loop_state"] == "polling"
            assert polling["last_order_poll_at"].endswith("Z")
            # A held claim lasts 20 s, longer than the stale bound: holding IS alive.
            commands.order_poll_liveness(_Req(app)).last_at = datetime.now(UTC) - timedelta(minutes=5)
            assert (await _mirror(client))["order_loop_state"] == "polling"

            submitted = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            claim = await held
            order_id = claim.json()["id"]
            # Claimed, not answered: a ramp can run a minute; that is not a dead loop.
            liveness = commands.order_poll_liveness(_Req(app))
            liveness.last_at = datetime.now(UTC) - timedelta(minutes=5)
            try:
                assert (await _mirror(client))["order_loop_state"] == "running_order"
            finally:
                # Always answer the order, so a failing assertion fails instead of hanging.
                done = await client.post(
                    f"/api/v1/commands/{order_id}/result",
                    json={"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}},
                )
                await submitted
            assert done.status_code == 202
            # Nothing held, nothing running, last poll 5 minutes ago: dead.
            assert (await _mirror(client))["order_loop_state"] == "stale"
            liveness.last_at = datetime.now(UTC)
            assert (await _mirror(client))["order_loop_state"] == "polling"

    asyncio.run(asyncio.wait_for(run(), 20))


class _Req:
    """The app-state half of a Request, for reading the liveness record."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app


def test_opendj_state_prints_the_order_loop() -> None:
    mirror = blank_mirror()
    mirror["last_order_poll_at"] = "2026-10-06T07:00:00.000Z"
    mirror["order_loop_state"] = "stale"
    assert "order loop: stale (last poll 2026-10-06T07:00:00.000Z)" in _state_text(mirror).splitlines()
    assert "order loop: absent" in _state_text(blank_mirror()).splitlines()
