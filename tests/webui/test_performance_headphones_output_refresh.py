"""Issue #12219: headphone output refresh refusal and recovery on the HTTP mirror.

[if] audio output refresh rejects [then] pending clears via a settled order, [else stop].
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from tests.webui.test_performance_headphones import (
    _DEFAULT_HEADPHONES,
    _app,
    _open_page,
)

pytestmark = pytest.mark.requirement("IOPIN-12219")

_REFRESH_PATH = "/api/v1/performance/headphones/outputs/refresh"
_ENUMERATION_ERROR = "enumeration failed: device hub offline"


async def _claim_and_fail_refresh(client: AsyncClient, error: str) -> None:
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
            "steps": [{"status": "failed", "error": error}],
            "mirror_delta": {
                "changed": {
                    "mixer": {
                        "headphones": {
                            "device_access": {
                                "status": "enumeration_failed",
                                "action": "retry",
                                "message": "Device enumeration failed.",
                                "detail": error,
                                "output_pinning": True,
                                "notices": [],
                            }
                        }
                    }
                }
            },
        },
    )


async def _claim_and_succeed_refresh(client: AsyncClient) -> None:
    for _ in range(200):
        await asyncio.sleep(0.01)
        nxt = await client.get("/api/v1/commands/next")
        if nxt.json() is not None:
            claimed = nxt.json()
            break
    else:
        raise AssertionError("order was never offered")
    outputs = [
        {"id": "default", "label": "Default"},
        {"id": "usb-audio-1", "label": "USB Audio"},
    ]
    await client.post(
        f"/api/v1/commands/{claimed['id']}/result",
        json={
            "steps": [{"status": "succeeded"}],
            "mirror_delta": {
                "changed": {
                    "mixer": {
                        "headphones": {
                            "outputs": outputs,
                            "device_access": {
                                "status": "listed",
                                "action": "none",
                                "message": "Audio outputs are listed.",
                                "detail": None,
                                "output_pinning": True,
                                "notices": [],
                            },
                        }
                    }
                }
            },
        },
    )


@pytest.mark.requirement("IOPIN-12219")
def test_outputs_refresh_failure_returns_named_error_and_clears_order_pending() -> None:
    """[if] refresh rejects [then] HTTP 400 names the error and the order loop is not running, [else stop]."""

    async def run() -> None:
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
            await _open_page(client, headphones={**_DEFAULT_HEADPHONES, "outputs": []})
            fake = asyncio.create_task(_claim_and_fail_refresh(client, _ENUMERATION_ERROR))
            response = await client.post(_REFRESH_PATH, json={})
            await fake
            mirror = await client.get("/api/v1/state/ui-mirror")
            got = await client.get("/api/v1/performance/headphones")

        assert response.status_code == 400
        assert response.json()["detail"] == _ENUMERATION_ERROR
        assert mirror.json()["order_loop_state"] != "running_order"
        assert got.json()["outputs"] == []

    asyncio.run(run())


@pytest.mark.requirement("IOPIN-12219")
def test_outputs_refresh_success_after_failure_replaces_error_state() -> None:
    """[if] the next refresh succeeds [then] listed outputs replace the failure state, [else stop]."""

    async def run() -> None:
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
            await _open_page(
                client,
                headphones={
                    **_DEFAULT_HEADPHONES,
                    "outputs": [],
                    "device_access": {
                        **_DEFAULT_HEADPHONES["device_access"],
                        "status": "enumeration_failed",
                        "detail": _ENUMERATION_ERROR,
                    },
                },
            )
            fail = asyncio.create_task(_claim_and_fail_refresh(client, _ENUMERATION_ERROR))
            failed = await client.post(_REFRESH_PATH, json={})
            await fail
            assert failed.status_code == 400

            ok = asyncio.create_task(_claim_and_succeed_refresh(client))
            succeeded = await client.post(_REFRESH_PATH, json={})
            await ok
            got = await client.get("/api/v1/performance/headphones")

        assert succeeded.status_code == 200
        body = got.json()
        assert body["device_access"]["status"] == "listed"
        assert body["device_access"]["detail"] is None
        assert len(body["outputs"]) == 2

    asyncio.run(run())
