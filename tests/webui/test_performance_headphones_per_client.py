"""GET /api/v1/performance/headphones is keyed per reporting client (CUEOUT-18).

Measured Thu 1 Oct 2026 on the live preview: an automation tab with the
microphone denied published `permission_denied` with 1 output and replaced the
operator tab's `listed` state with 9 outputs, because the route read the one
last-writer-wins UI mirror. These tests drive the real state and headphones
routers; the only injected part is the report store's clock.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from apps.webui.server.headphone_reports import (
    ANONYMOUS_CLIENT_ID,
    HEADPHONE_REPORT_STALE_S,
    HeadphoneReports,
)
from tests.webui.test_performance_headphones import _DEFAULT_CHANNELS, _DEFAULT_HEADPHONES, _app

_NINE_OUTPUTS = [{"id": f"out-{n}", "label": f"Output {n}"} for n in range(9)]
_ONE_OUTPUT = [{"id": "default", "label": "System default output"}]


def _headphones(status: str, outputs: list[dict[str, str]], **extra: Any) -> dict[str, Any]:
    headphones = dict(_DEFAULT_HEADPHONES)
    headphones["outputs"] = outputs
    headphones["device_access"] = {**_DEFAULT_HEADPHONES["device_access"], "status": status}
    headphones.update(extra)
    return headphones


async def _publish(client: AsyncClient, client_id: str | None, headphones: dict[str, Any]) -> None:
    body: dict[str, Any] = {
        "client_open": True,
        "mixer": {"headphones": headphones, "channels": dict(_DEFAULT_CHANNELS)},
    }
    if client_id is not None:
        body["client_id"] = client_id
    published = await client.put("/api/v1/state/ui-mirror", json=body)
    assert published.status_code == 202


class _Clock:
    def __init__(self) -> None:
        self.now_s = 1000.0

    def __call__(self) -> float:
        return self.now_s


def _app_with_clock() -> tuple[Any, _Clock]:
    app = _app()
    clock = _Clock()
    app.state.headphone_reports = HeadphoneReports(now=clock)
    return app, clock


@pytest.mark.requirement("CUEOUT-18")
def test_a_denied_report_from_another_client_does_not_replace_a_listed_one() -> None:
    """[if] a denied tab publishes after the operator's tab [then] GET still returns listed with 9 outputs, [else stop]."""

    async def run() -> None:
        app, clock = _app_with_clock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _publish(client, "operator-tab", _headphones("listed", _NINE_OUTPUTS))
            clock.now_s += 1.5
            await _publish(client, "automation-tab", _headphones("permission_denied", _ONE_OUTPUT))
            clock.now_s += 0.25
            got = await client.get("/api/v1/performance/headphones")
            mirror = await client.get("/api/v1/state/ui-mirror")
        assert got.status_code == 200
        body = got.json()
        assert body["device_access"]["status"] == "listed"
        assert len(body["outputs"]) == 9
        assert body["reporting_client_id"] == "operator-tab"
        assert body["report_age_ms"] == pytest.approx(1750.0)
        # Control: the mirror itself is still last-writer-wins, so the old read
        # (mixer.headphones off the mirror) would have answered permission_denied.
        assert mirror.json()["mixer"]["headphones"]["device_access"]["status"] == "permission_denied"

    asyncio.run(run())


@pytest.mark.requirement("CUEOUT-18")
def test_not_checked_from_another_client_does_not_replace_listed() -> None:
    """[if] a freshly opened tab publishes not_checked [then] the listed client is still returned, [else stop]."""

    async def run() -> None:
        app, clock = _app_with_clock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _publish(client, "operator-tab", _headphones("listed", _NINE_OUTPUTS))
            clock.now_s += 1.0
            await _publish(client, "new-tab", _headphones("not_checked", []))
            got = await client.get("/api/v1/performance/headphones")
        assert got.json()["reporting_client_id"] == "operator-tab"

    asyncio.run(run())


@pytest.mark.requirement("CUEOUT-18")
def test_a_clients_own_later_report_replaces_its_earlier_one() -> None:
    """[if] the listed client itself later reports permission_denied [then] GET returns that, [else stop].

    Overshoot control: the rank must not freeze a state its own owner withdrew.
    """

    async def run() -> None:
        app, clock = _app_with_clock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _publish(client, "operator-tab", _headphones("listed", _NINE_OUTPUTS))
            clock.now_s += 1.0
            await _publish(client, "operator-tab", _headphones("permission_denied", _ONE_OUTPUT))
            got = await client.get("/api/v1/performance/headphones")
        body = got.json()
        assert body["device_access"]["status"] == "permission_denied"
        assert len(body["outputs"]) == 1
        assert body["reporting_client_id"] == "operator-tab"
        assert body["report_age_ms"] == pytest.approx(0.0)

    asyncio.run(run())


@pytest.mark.requirement("CUEOUT-18")
def test_between_two_listed_clients_the_latest_report_wins() -> None:
    """[if] two clients both report listed [then] the most recent report is returned, [else stop]."""

    async def run() -> None:
        app, clock = _app_with_clock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _publish(client, "tab-a", _headphones("listed", _NINE_OUTPUTS, mix=0.2))
            clock.now_s += 1.0
            await _publish(client, "tab-b", _headphones("listed", _NINE_OUTPUTS[:4], mix=0.8))
            got = await client.get("/api/v1/performance/headphones")
        body = got.json()
        assert body["reporting_client_id"] == "tab-b"
        assert body["mix"] == 0.8

    asyncio.run(run())


@pytest.mark.requirement("CUEOUT-18")
def test_a_stale_listed_client_expires_and_a_live_client_takes_over() -> None:
    """[if] the listed client goes silent past the stale bound [then] the live client is returned, [else stop]."""

    async def run() -> None:
        app, clock = _app_with_clock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _publish(client, "closed-tab", _headphones("listed", _NINE_OUTPUTS))
            clock.now_s += HEADPHONE_REPORT_STALE_S - 1.0
            await _publish(client, "live-tab", _headphones("permission_denied", _ONE_OUTPUT))
            inside = await client.get("/api/v1/performance/headphones")
            clock.now_s += 2.0
            await _publish(client, "live-tab", _headphones("permission_denied", _ONE_OUTPUT))
            outside = await client.get("/api/v1/performance/headphones")
        assert inside.json()["reporting_client_id"] == "closed-tab", "control: not expired inside the bound"
        assert outside.json()["reporting_client_id"] == "live-tab"
        assert outside.json()["device_access"]["status"] == "permission_denied"

    asyncio.run(run())


@pytest.mark.requirement("CUEOUT-18")
def test_closing_a_client_forgets_only_that_client() -> None:
    """[if] the listed tab closes [then] its report is gone at once and the other tab's report is returned once it publishes again, [else stop]."""

    async def run() -> None:
        app, clock = _app_with_clock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _publish(client, "operator-tab", _headphones("listed", _NINE_OUTPUTS))
            await _publish(client, "automation-tab", _headphones("permission_denied", _ONE_OUTPUT))
            closed = await client.delete(
                "/api/v1/state/ui-mirror", headers={"x-opendj-client-id": "operator-tab"}
            )
            assert closed.status_code == 204
            clock.now_s += 1.0
            await _publish(client, "automation-tab", _headphones("permission_denied", _ONE_OUTPUT))
            got = await client.get("/api/v1/performance/headphones")
        assert got.json()["reporting_client_id"] == "automation-tab"

    asyncio.run(run())


@pytest.mark.requirement("CUEOUT-18")
def test_a_page_that_sends_no_client_id_still_reads_back() -> None:
    """[if] an older page publishes with no client_id [then] GET answers it as the anonymous client, [else stop]."""

    async def run() -> None:
        app, _clock = _app_with_clock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _publish(client, None, _headphones("listed", _NINE_OUTPUTS))
            got = await client.get("/api/v1/performance/headphones")
        body = got.json()
        assert got.status_code == 200
        assert body["reporting_client_id"] == ANONYMOUS_CLIENT_ID
        assert set(_DEFAULT_HEADPHONES) <= set(body), "the earlier route shape must be intact"

    asyncio.run(run())


def test_the_store_ranks_listed_above_every_other_status() -> None:
    """[if] any non-listed status outranks listed [then] a tab that cannot enumerate hides one that can, [else stop]."""
    for other in ("not_checked", "permission_needed", "permission_denied", "api_missing", "enumeration_failed", "timeout"):
        clock = _Clock()
        reports = HeadphoneReports(now=clock)
        reports.record("listed-tab", _headphones("listed", _NINE_OUTPUTS))
        clock.now_s += 1.0
        reports.record("other-tab", _headphones(other, _ONE_OUTPUT))
        best = reports.best()
        assert best is not None and best.client_id == "listed-tab", other


def test_with_every_client_stale_the_freshest_is_returned_with_its_real_age() -> None:
    """[if] nothing has reported inside the bound [then] the freshest report is returned with its true age, [else stop]."""
    clock = _Clock()
    reports = HeadphoneReports(now=clock)
    reports.record("tab", _headphones("listed", _NINE_OUTPUTS))
    clock.now_s += HEADPHONE_REPORT_STALE_S * 3
    best = reports.best()
    assert best is not None
    assert reports.age_ms(best) == pytest.approx(HEADPHONE_REPORT_STALE_S * 3000)
