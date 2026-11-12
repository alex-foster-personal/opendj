"""[if] link.clock/ws protocol changes [then] tests cover clock, ping and flag-off close, [else stop]."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apps.ableton_link.carabiner import CarabinerClient
from apps.b2b_sync.link_roles import RoleAuthority
from apps.engine_core.link_clock_ws import (
    LINK_CLOCK_POLICY_ERROR_CODE,
    LINK_CLOCK_WS_PATH,
    LINK_FLAG_ID,
    _LINK_CLIENT_KEY,
    build_link_ping_body,
    link_clock_body_for_app,
    link_clock_endpoint,
)
from apps.engine_core.ws import HELLO_TOPIC, WsHub, events_endpoint
from tests.ableton_link.carabiner_sim import SimCarabiner
from tests.support.process_probes import wait_for

pytestmark = [pytest.mark.requirement("LINK-12783")]

EVENTS_PATH = "/api/v1/events"
_FROZEN_LINK_NOW_US = 9_876_543_210


class _FlagStore:
    def __init__(self, *, link_on: bool) -> None:
        self._link_on = link_on

    def enabled(self, flag_id: str) -> bool:
        if flag_id != LINK_FLAG_ID:
            raise KeyError(flag_id)
        return self._link_on


def _app(*, link_on: bool, hub: WsHub | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if hub is not None:
            hub.bind(asyncio.get_running_loop())
        yield
        if hub is not None:
            hub.unbind()

    app = FastAPI(lifespan=lifespan)
    app.state.feature_flags = _FlagStore(link_on=link_on)
    if hub is not None:
        app.state.event_hub = hub
        app.add_api_websocket_route(EVENTS_PATH, events_endpoint, name="events")
    app.add_api_websocket_route(LINK_CLOCK_WS_PATH, link_clock_endpoint, name="link_clock")
    return app


def test_flag_off_closes_socket_with_1008() -> None:
    """[if] link.tempo_sync is off [then] the socket closes 1008 with the refusal reason, [else stop]."""
    app = _app(link_on=False)
    with TestClient(app) as client, client.websocket_connect(LINK_CLOCK_WS_PATH) as ws:
        with pytest.raises(WebSocketDisconnect) as raised:
            ws.receive_text()
    assert raised.value.code == LINK_CLOCK_POLICY_ERROR_CODE
    assert LINK_FLAG_ID in (raised.value.reason or "")


def test_clock_op_carries_id_and_matches_http_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] op clock arrives [then] the answer carries id and matches the HTTP clock body, [else stop]."""
    monkeypatch.setattr(
        "apps.engine_core.link_clock_ws.link_now_us",
        lambda: _FROZEN_LINK_NOW_US,
    )
    app = _app(link_on=True)
    authority = RoleAuthority("engine-a")
    app.state.link_b2b_role = authority

    with TestClient(app) as client, client.websocket_connect(LINK_CLOCK_WS_PATH) as ws:
        ws.send_text(json.dumps({"id": "c1", "op": "clock"}))
        answer = json.loads(ws.receive_text())

    assert answer["id"] == "c1"
    expected_http = asyncio.run(link_clock_body_for_app(app))
    body = {key: value for key, value in answer.items() if key != "id"}
    assert body == expected_http


def test_ping_op_carries_id_and_matches_http_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] op ping arrives [then] the answer carries id and matches the HTTP ping body, [else stop]."""
    monkeypatch.setattr(
        "apps.engine_core.link_clock_ws.link_now_us",
        lambda: _FROZEN_LINK_NOW_US,
    )
    app = _app(link_on=True)

    with TestClient(app) as client, client.websocket_connect(LINK_CLOCK_WS_PATH) as ws:
        ws.send_text(json.dumps({"id": "p1", "op": "ping"}))
        answer = json.loads(ws.receive_text())

    assert answer["id"] == "p1"
    assert answer == {"id": "p1", **build_link_ping_body()}


def test_joined_but_silent_carabiner_returns_503_on_socket() -> None:
    """[if] the handler would return 503 over HTTP [then] the socket answers id, status and detail, [else stop]."""
    app = _app(link_on=True)
    client = CarabinerClient(port=19997)
    setattr(app.state, _LINK_CLIENT_KEY, client)
    detail = f"Carabiner on {client.address} has not reported the session yet"

    with TestClient(app) as client_http, client_http.websocket_connect(LINK_CLOCK_WS_PATH) as ws:
        ws.send_text(json.dumps({"id": 7, "op": "clock"}))
        answer = json.loads(ws.receive_text())

    assert answer == {"id": 7, "status": 503, "detail": detail}


def test_connected_session_clock_matches_snapshot_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] Carabiner is live [then] the WS clock body matches link_clock_body_for_app, [else stop]."""
    monkeypatch.setattr(
        "apps.engine_core.link_clock_ws.link_now_us",
        lambda: _FROZEN_LINK_NOW_US,
    )
    sim = SimCarabiner(bpm=128.0, peers=1, now_us=lambda: _FROZEN_LINK_NOW_US)
    link_client = CarabinerClient(port=sim.port)
    link_client.start()
    wait_for(lambda: link_client.snapshot().connected, timeout_s=3.0, what="carabiner status")

    app = _app(link_on=True)
    setattr(app.state, _LINK_CLIENT_KEY, link_client)
    with TestClient(app) as client, client.websocket_connect(LINK_CLOCK_WS_PATH) as ws:
        ws.send_text(json.dumps({"id": "live", "op": "clock"}))
        answer = json.loads(ws.receive_text())
        expected = asyncio.run(link_clock_body_for_app(app))

    link_client.stop()
    sim.close()

    body = {key: value for key, value in answer.items() if key != "id"}
    assert body == expected


def test_link_clock_socket_is_not_the_events_hub() -> None:
    """[if] two pages open clock and events sockets [then] neither shares the other's connection, [else stop]."""
    hub = WsHub(contract_rev="rev", engine_version="0.0.0")
    app = _app(link_on=True, hub=hub)
    with TestClient(app) as client:
        with client.websocket_connect(EVENTS_PATH) as events_ws:
            hello = json.loads(events_ws.receive_text())
            assert hello["topic"] == HELLO_TOPIC
            with client.websocket_connect(LINK_CLOCK_WS_PATH) as clock_ws:
                clock_ws.send_text(json.dumps({"id": "x", "op": "ping"}))
                ping = json.loads(clock_ws.receive_text())
                assert ping["id"] == "x"
                assert "link_now_us" in ping
