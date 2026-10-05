"""AGENT-18: one leased writer of the UI mirror per engine.

Mon 5 Oct 2026, live preview: three /performance tabs (the maintainer's Chrome and two
agent browser panes) each PUT the mirror every second, so the engine's view of
which deck was playing flipped between tabs and a stopped tab overwrote the
playing one. Web Locks fix that inside one browser; this lease is the part that
works across browsers. These tests drive the real state router; only the
lease clock is patched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from apps.webui.server.routes import state as state_routes


class _Clock:
    def __init__(self) -> None:
        self.now_s = 500.0

    def __call__(self) -> float:
        return self.now_s


def _run(
    monkeypatch: pytest.MonkeyPatch,
    body: Callable[[AsyncClient, _Clock], Awaitable[None]],
) -> None:
    clock = _Clock()
    monkeypatch.setattr(state_routes, "monotonic", clock)
    app = FastAPI()
    app.include_router(state_routes.router, prefix="/api/v1")

    async def run() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await body(client, clock)

    asyncio.run(run())


async def _put(client: AsyncClient, tab: str, *, takeover: bool = False, playing: bool = False) -> Response:
    headers = {"x-opendj-lease": tab}
    if takeover:
        headers["x-opendj-lease-takeover"] = "1"
    payload: dict[str, Any] = {"client_open": True, "client_id": tab, "decks": {"1": {"playing": playing}}}
    return await client.put("/api/v1/state/ui-mirror", json=payload, headers=headers)


@pytest.mark.requirement("AGENT-18")
def test_a_second_leased_tab_is_refused_and_cannot_overwrite(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] B publishes while A holds the lease [then] B gets 409, A's mirror stays, [else stop]."""

    async def body(client: AsyncClient, clock: _Clock) -> None:
        assert (await _put(client, "tab-a", playing=True)).status_code == 202
        clock.now_s += 1
        refused = await _put(client, "tab-b", playing=False)
        assert refused.status_code == 409
        assert refused.json()["reason"] == "lease_held"
        assert refused.json()["holder"] == "tab-a"
        mirror = (await client.get("/api/v1/state/ui-mirror")).json()
        assert mirror["client_id"] == "tab-a"
        assert mirror["decks"]["1"]["playing"] is True

    _run(monkeypatch, body)


@pytest.mark.requirement("AGENT-18")
def test_the_holder_keeps_renewing(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the holder republishes each second for 30 s [then] it is never refused, [else stop]."""

    async def body(client: AsyncClient, clock: _Clock) -> None:
        for _ in range(30):
            assert (await _put(client, "tab-a")).status_code == 202
            clock.now_s += 1
        assert (await _put(client, "tab-b")).status_code == 409

    _run(monkeypatch, body)


@pytest.mark.requirement("AGENT-18")
def test_a_lapsed_lease_hands_over(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the holder stops past the TTL [then] the next tab takes the lease, [else stop]."""

    async def body(client: AsyncClient, clock: _Clock) -> None:
        assert (await _put(client, "tab-a")).status_code == 202
        clock.now_s += state_routes.LEASE_TTL_S - 0.5
        assert (await _put(client, "tab-b")).status_code == 409, "control: still held inside the TTL"
        clock.now_s += 1.0
        assert (await _put(client, "tab-b")).status_code == 202
        lease = (await client.get("/api/v1/state/ui-mirror/lease")).json()
        assert lease["held"] is True
        assert lease["holder"] == "tab-b"

    _run(monkeypatch, body)


@pytest.mark.requirement("AGENT-18")
def test_closing_the_holder_releases_the_lease_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the holder DELETEs the mirror [then] another tab can publish at once, [else stop]."""

    async def body(client: AsyncClient, clock: _Clock) -> None:
        assert (await _put(client, "tab-a")).status_code == 202
        closed = await client.delete("/api/v1/state/ui-mirror", headers={"x-opendj-client-id": "tab-a"})
        assert closed.status_code == 204
        assert (await client.get("/api/v1/state/ui-mirror/lease")).json()["held"] is False
        assert (await _put(client, "tab-b")).status_code == 202

    _run(monkeypatch, body)


@pytest.mark.requirement("AGENT-18")
def test_a_follower_closing_does_not_blank_the_leaders_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a non-holder DELETEs the mirror [then] the holder's mirror is kept, [else stop]."""

    async def body(client: AsyncClient, clock: _Clock) -> None:
        assert (await _put(client, "tab-a", playing=True)).status_code == 202
        closed = await client.delete("/api/v1/state/ui-mirror", headers={"x-opendj-client-id": "tab-b"})
        assert closed.status_code == 204
        mirror = await client.get("/api/v1/state/ui-mirror")
        assert mirror.status_code == 200
        assert mirror.json()["client_id"] == "tab-a"

    _run(monkeypatch, body)


@pytest.mark.requirement("AGENT-18")
def test_take_control_steals_the_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] B publishes with takeover [then] B holds the lease and A is refused, [else stop]."""

    async def body(client: AsyncClient, clock: _Clock) -> None:
        assert (await _put(client, "tab-a")).status_code == 202
        assert (await _put(client, "tab-b", takeover=True)).status_code == 202
        assert (await _put(client, "tab-a")).status_code == 409
        lease = (await client.get("/api/v1/state/ui-mirror/lease")).json()
        assert lease["holder"] == "tab-b"
        assert 0 < lease["expires_in_ms"] <= lease["ttl_ms"]

    _run(monkeypatch, body)


@pytest.mark.requirement("AGENT-18")
def test_unleased_publishes_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a PUT has no lease header [then] it is accepted as before, [else stop]."""

    async def body(client: AsyncClient, clock: _Clock) -> None:
        assert (await _put(client, "tab-a")).status_code == 202
        legacy = await client.put("/api/v1/state/ui-mirror", json={"client_open": True, "client_id": "agent"})
        assert legacy.status_code == 202
        assert (await client.get("/api/v1/state/ui-mirror/lease")).json()["holder"] == "tab-a"

    _run(monkeypatch, body)


@pytest.mark.requirement("AGENT-18")
def test_a_lease_header_that_disagrees_with_the_body_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the lease header and body client_id differ [then] the PUT is a 422, [else stop]."""

    async def body(client: AsyncClient, clock: _Clock) -> None:
        put = await client.put(
            "/api/v1/state/ui-mirror",
            json={"client_open": True, "client_id": "tab-a"},
            headers={"x-opendj-lease": "tab-b"},
        )
        assert put.status_code == 422
        assert (await client.get("/api/v1/state/ui-mirror/lease")).json()["held"] is False

    _run(monkeypatch, body)


async def _put_at(client: AsyncClient, tab: str, published_at: str, *, playing: bool) -> Response:
    payload = {"client_open": True, "client_id": tab, "published_at": published_at, "decks": {"1": {"playing": playing}}}
    return await client.put("/api/v1/state/ui-mirror", json=payload)


@pytest.mark.requirement("AGENT-18")
def test_a_stale_snapshot_never_replaces_a_fresher_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[if] a PUT is older than the stored snapshot [then] 409 stale_snapshot, logged, [else stop]."""

    async def body(client: AsyncClient, clock: _Clock) -> None:
        assert (await _put_at(client, "live", "2026-10-05T20:04:00.000Z", playing=True)).status_code == 202
        clock.now_s += 1
        stale = await _put_at(client, "dead-page", "2026-10-05T19:59:00.000Z", playing=False)
        assert stale.status_code == 409
        assert stale.json()["reason"] == "stale_snapshot"
        mirror = (await client.get("/api/v1/state/ui-mirror")).json()
        assert mirror["client_id"] == "live"
        assert mirror["decks"]["1"]["playing"] is True
        assert (await _put_at(client, "live", "2026-10-05T20:04:01.000Z", playing=True)).status_code == 202

    with caplog.at_level("WARNING"):
        _run(monkeypatch, body)
    assert any("stale_snapshot" in r.message and "dead-page" in r.message for r in caplog.records)


@pytest.mark.requirement("AGENT-18")
def test_an_old_stored_snapshot_yields_to_any_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the stored snapshot is past the TTL [then] a skewed-clock writer is accepted, [else stop]."""

    async def body(client: AsyncClient, clock: _Clock) -> None:
        assert (await _put_at(client, "a", "2026-10-05T20:04:00.000Z", playing=False)).status_code == 202
        clock.now_s += state_routes.LEASE_TTL_S - 1
        assert (await _put_at(client, "b", "2026-10-05T20:03:59.000Z", playing=True)).status_code == 409
        clock.now_s += 2
        assert (await _put_at(client, "b", "2026-10-05T20:03:59.500Z", playing=True)).status_code == 202

    _run(monkeypatch, body)


async def _put_tab(
    client: AsyncClient, tab: str, *, playing: bool, visible: bool = True, takeover: bool = False
) -> Response:
    headers = {"x-opendj-lease": tab}
    if takeover:
        headers["x-opendj-lease-takeover"] = "1"
    payload = {
        "client_open": True,
        "client_id": tab,
        "tab": {"visible": visible},
        "decks": {"1": {"playing": playing}, "2": {"playing": False}},
    }
    return await client.put("/api/v1/state/ui-mirror", json=payload, headers=headers)


@pytest.mark.requirement("AGENT-18")
def test_a_playing_claimant_takes_the_lease_from_a_silent_holder(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[if] the holder is silent and a playing tab publishes [then] it takes the lease, [else stop]."""

    async def body(client: AsyncClient, clock: _Clock) -> None:
        assert (await _put_tab(client, "idle-chrome", playing=False)).status_code == 202
        lease = (await client.get("/api/v1/state/ui-mirror/lease")).json()
        assert lease["holder_playing"] is False
        assert (await _put_tab(client, "agent-pane", playing=True)).status_code == 202
        lease = (await client.get("/api/v1/state/ui-mirror/lease")).json()
        assert lease["holder"] == "agent-pane"
        assert lease["holder_playing"] is True
        assert (await _put_tab(client, "idle-chrome", playing=False)).status_code == 409

    with caplog.at_level("WARNING"):
        _run(monkeypatch, body)
    assert any("audible_over_silent" in r.message and "agent-pane" in r.message for r in caplog.records)


@pytest.mark.requirement("AGENT-18")
def test_a_playing_holder_keeps_the_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the holder is playing [then] another playing or idle tab is refused, [else stop]."""

    async def body(client: AsyncClient, clock: _Clock) -> None:
        assert (await _put_tab(client, "a", playing=True, visible=False)).status_code == 202
        assert (await _put_tab(client, "b", playing=True)).status_code == 409
        assert (await _put_tab(client, "b", playing=False)).status_code == 409

    _run(monkeypatch, body)


@pytest.mark.requirement("AGENT-18")
def test_a_hidden_idle_holder_yields_to_a_visible_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the holder is hidden and silent [then] a visible idle tab takes the lease, [else stop]."""

    async def body(client: AsyncClient, clock: _Clock) -> None:
        assert (await _put_tab(client, "a", playing=False, visible=False)).status_code == 202
        assert (await client.get("/api/v1/state/ui-mirror/lease")).json()["holder_yieldable"] is True
        assert (await _put_tab(client, "c", playing=False, visible=False)).status_code == 409, "control"
        assert (await _put_tab(client, "b", playing=False, visible=True)).status_code == 202
        assert (await client.get("/api/v1/state/ui-mirror/lease")).json()["holder"] == "b"

    _run(monkeypatch, body)


@pytest.mark.requirement("AGENT-18")
def test_a_visible_idle_holder_is_not_displaced_by_an_idle_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] holder and claimant are both visible and silent [then] the claimant is refused, [else stop]."""

    async def body(client: AsyncClient, clock: _Clock) -> None:
        assert (await _put_tab(client, "a", playing=False)).status_code == 202
        assert (await _put_tab(client, "b", playing=False)).status_code == 409

    _run(monkeypatch, body)
