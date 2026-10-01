"""GET /api/v1/performance/headphones carries the device list and its reason (IOPIN-14).

Split from test_performance_headphones.py, whose app factory and page opener
it reuses, to keep that module under the file-size limit.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from tests.webui.test_performance_headphones import _DEFAULT_HEADPHONES, _app, _open_page


@pytest.mark.requirement("IOPIN-14")
def test_get_returns_the_device_list_and_why_it_is_short() -> None:
    """[if] GET drops device_access or the outputs [then] an agent reads a short list as a machine with few devices, [else stop]."""

    async def run() -> None:
        headphones = dict(_DEFAULT_HEADPHONES)
        headphones["outputs"] = [{"id": "default", "label": "System default output"}]
        headphones["device_access"] = {
            "status": "permission_denied",
            "action": "retry",
            "message": "Audio device access is blocked, so only the system default output can be listed.",
            "detail": None,
            "output_pinning": True,
            "notices": [],
        }
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            await _open_page(client, headphones=headphones)
            got = await client.get("/api/v1/performance/headphones")
        assert got.status_code == 200
        body = got.json()
        assert body["outputs"] == headphones["outputs"]
        assert body["device_access"] == headphones["device_access"]

    asyncio.run(run())
