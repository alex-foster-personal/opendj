"""CUEOUT-15 library preview routes: body validation and page refusals.

Split from test_performance_headphones.py, which holds the shared headphone
route harness, so neither file passes the 600-line gate.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from tests.webui.test_performance_headphones import _app, _open_page


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"ratio": 0.5}, "stable_id must be a non-empty string"),
        ({"stable_id": "   ", "ratio": 0.5}, "stable_id must be a non-empty string"),
        ({"stable_id": 7, "ratio": 0.5}, "stable_id must be a non-empty string"),
        ({"stable_id": "trk-9", "ratio": 1.5}, "value must be within 0..1"),
        ({"stable_id": "trk-9", "ratio": "half"}, "value must be a finite number"),
        (
            {"stable_id": "trk-9", "ratio": 0.5, "bpm": 0},
            "bpm must be a positive finite number",
        ),
        (
            {"stable_id": "trk-9", "ratio": 0.5, "bpm": "128"},
            "bpm must be a positive finite number",
        ),
    ],
)
def test_preview_bodies_are_rejected_without_submitting(
    body: dict[str, Any], detail: str
) -> None:
    """CUEOUT-15: a malformed preview never reaches the page.

    The point is the "without submitting" half: an order that the page would
    only fail on arrival still occupies the single-command queue, so every
    check that can be made here is made here.
    """

    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            await _open_page(client)
            response = await client.post(
                "/api/v1/performance/headphones/preview", json=body
            )
            assert response.status_code == 400
            assert detail in response.json()["detail"]
            nxt = await client.get("/api/v1/commands/next")
            assert nxt.json() is None

    asyncio.run(run())


def test_preview_refused_by_the_page_is_a_400_carrying_the_reason() -> None:
    """CUEOUT-15: a preview nobody could hear must never answer 200.

    The page refuses for reasons only it can know (no audio graph, a dead cue
    sink, MIX at the master end, GAIN at zero). Those arrive as a failed step,
    and the operator-facing wording is what the caller gets back.
    """
    refusal = "preview: the cue path is silent - raise MIX (turn it toward CUE) or GAIN"

    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            await _open_page(client)

            async def claim_and_refuse() -> None:
                for _ in range(200):
                    await asyncio.sleep(0.01)
                    nxt = await client.get("/api/v1/commands/next")
                    if nxt.json() is not None:
                        claimed = nxt.json()
                        break
                else:
                    raise AssertionError("order was never offered")
                await client.post(
                    f"/api/v1/commands/{claimed['id']}/result",
                    json={
                        "steps": [{"status": "failed", "error": refusal}],
                        "mirror_delta": {"changed": {}},
                    },
                )

            fake = asyncio.create_task(claim_and_refuse())
            response = await client.post(
                "/api/v1/performance/headphones/preview",
                json={"stable_id": "trk-9", "ratio": 0.5},
            )
            await fake
            assert response.status_code == 400
            assert response.json()["detail"] == refusal

    asyncio.run(run())


def test_preview_bpm_reaches_the_page_for_the_tempo_match() -> None:
    """CUEOUT-15 R6: the caller's BPM is forwarded, not dropped.

    Tempo matching is decided in the page (it is the only place that knows
    which deck is master and playing), so the BPM has to survive the route.
    A preview sent without one is still a valid preview and carries no key.
    """

    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            await _open_page(client)
            task = asyncio.create_task(
                client.post(
                    "/api/v1/performance/headphones/preview",
                    json={"stable_id": "trk-9", "ratio": 0.5, "bpm": 128.5},
                )
            )
            await asyncio.sleep(0)
            nxt = await client.get("/api/v1/commands/next")
            command = nxt.json()
            assert command is not None
            assert command["payload"]["bpm"] == 128.5, (
                "if the route drops bpm then no preview started by an agent can "
                "ever tempo-match - broken"
            )
            task.cancel()

    asyncio.run(run())
