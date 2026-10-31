"""I18 remote speaker: the engine half (REMOTE-SPK-8119..8119h).

The LAN listener and the relay are run for real: a real uvicorn listener on a
real loopback socket, a real httpx relay from a second (sender) engine. The
page is the only stand-in: a task that claims the engine's real command
orders and answers them with a fixed SDP, because media is the browser's job
(the two-Chromium proof is tests/live/remote-speaker-loopback.mjs).

[if] the code is wrong [then ⛔] 403, and the fifth wrong code closes the listener.
[if] any other path is asked of the listener [then ⛔] 404: it exposes ONE route.
[if] the speaker stops [then ⛔] its port refuses connections.

[if] the code is right [then] the offer reaches the page and its answer comes back, [else stop].
[if] a sender links [then] its engine relays to a second real listener and reads linked, [else stop].
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Response
from httpx import ASGITransport, AsyncClient

from apps.webui.server import remote_speaker
from apps.webui.server.remote_speaker import (
    MAX_WRONG_CODES,
    OFFER_PATH,
    SpeakerSession,
    build_listener_app,
    parse_speaker_address,
    require_lan_host,
)
from apps.webui.server.routes.commands import router as commands_router
from apps.webui.server.routes.performance_remote_speaker import (
    REMOTE_SPEAKER_PAGE_COMMANDS,
    close_remote_speaker,
)
from apps.webui.server.routes.performance_remote_speaker import router as remote_speaker_router
from apps.webui.server.routes.state import router as state_router

pytestmark = pytest.mark.requirement("REMOTE-SPK-8119b")

BASE = "/api/v1/performance/remote-speaker"
OFFER_SDP = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=offer\r\n"
ANSWER_SDP = "v=0\r\no=- 3 4 IN IP4 127.0.0.1\r\ns=answer\r\n"

Handler = Callable[[dict[str, Any]], Any]


def _engine() -> FastAPI:
    app = FastAPI()
    app.include_router(state_router, prefix="/api/v1")
    app.include_router(commands_router, prefix="/api/v1")
    app.include_router(remote_speaker_router, prefix="/api/v1")
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://engine", timeout=60)


async def _open_page(client: AsyncClient) -> None:
    published = await client.put("/api/v1/state/ui-mirror", json={"client_open": True})
    assert published.status_code == 202


@asynccontextmanager
async def _page(client: AsyncClient, handle: Handler) -> AsyncIterator[list[dict[str, Any]]]:
    """A page that claims every order and answers it with ``handle(command)``.

    ``handle`` returns a step result dict (``{"sdp": ...}``), None for a plain
    success, or raises to report a failed step. Yields the commands it ran.
    """
    seen: list[dict[str, Any]] = []

    async def loop() -> None:
        while True:
            claimed = await client.get("/api/v1/commands/next", params={"wait_ms": 200})
            assert claimed.status_code == 200, claimed.text
            order = claimed.json()
            if order is None:
                continue
            command = order["payload"]
            seen.append(command)
            try:
                outcome = handle(command)
                if asyncio.iscoroutine(outcome):
                    outcome = await outcome
                step: dict[str, Any] = {"status": "succeeded"}
                if outcome is not None:
                    step["result"] = outcome
            except Exception as error:  # noqa: BLE001 -- the page reports any failure
                step = {"status": "failed", "error": str(error)}
            await client.post(
                f"/api/v1/commands/{order['id']}/result",
                json={"steps": [step], "mirror_delta": {"changed": {}}},
            )

    task = asyncio.create_task(loop())
    try:
        yield seen
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _receiver_handler(command: dict[str, Any]) -> dict[str, Any] | None:
    if command["type"] == "remote_speaker_answer":
        assert command["sdp"] == OFFER_SDP
        return {"sdp": ANSWER_SDP}
    return None


def _port_refuses(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _offer_url(port: int) -> str:
    return f"http://127.0.0.1:{port}{OFFER_PATH}"


async def _start_speaker(client: AsyncClient) -> dict[str, Any]:
    started = await client.post(f"{BASE}/speaker/start", json={"port": 0})
    assert started.status_code == 200, started.text
    return started.json()


def test_route_table_names_every_page_command() -> None:
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "apps/webui/frontend/src/lib/player/remote-speaker-orders.ts"
    ).read_text(encoding="utf-8")
    for command_type in REMOTE_SPEAKER_PAGE_COMMANDS:
        assert f"'{command_type}'" in source, f"the page does not handle {command_type}"


@pytest.mark.requirement("REMOTE-SPK-8119")
def test_start_opens_listener_with_code_and_right_code_links() -> None:
    """[if] the right code offers [then] the page answers and the speaker reads linked, [else stop]."""
    async def run() -> None:
        app = _engine()
        async with _client(app) as client:
            await _open_page(client)
            async with _page(client, _receiver_handler) as seen:
                state = await _start_speaker(client)
                try:
                    assert state["status"] == "listening"
                    assert len(state["code"]) == 6 and state["code"].isdigit()
                    assert isinstance(state["port"], int) and state["port"] > 0
                    assert "delayed" in state["delay_note"]
                    async with httpx.AsyncClient(trust_env=False, timeout=30) as lan:
                        answered = await lan.post(
                            _offer_url(state["port"]), json={"code": state["code"], "sdp": OFFER_SDP}
                        )
                        assert answered.status_code == 200, answered.text
                        assert answered.json() == {"sdp": ANSWER_SDP}
                        linked = (await client.get(f"{BASE}/speaker")).json()
                        assert linked["status"] == "linked"
                        assert linked["peer"] == "127.0.0.1"
                        # One active link per session.
                        second = await lan.post(
                            _offer_url(state["port"]), json={"code": state["code"], "sdp": OFFER_SDP}
                        )
                        assert second.status_code == 409
                    assert [c["type"] for c in seen] == ["remote_speaker_receive", "remote_speaker_answer"]
                    answered_with = seen[-1]
                    assert answered_with["epoch"] and answered_with["link_id"]
                    # A report about some OTHER (older) link changes nothing.
                    stale = await client.post(
                        f"{BASE}/speaker/report", json={"state": "closed", "link_id": "an-older-link"}
                    )
                    assert stale.json()["status"] == "linked"
                    # A WebRTC "disconnected" can recover on its own: still linked.
                    blip = await client.post(
                        f"{BASE}/speaker/report", json={"state": "disconnected", "link_id": answered_with["link_id"]}
                    )
                    assert blip.json()["status"] == "linked"
                    # The sender went away: the same code takes a new link.
                    reported = await client.post(
                        f"{BASE}/speaker/report", json={"state": "closed", "link_id": answered_with["link_id"]}
                    )
                    assert reported.json()["status"] == "listening"
                    assert reported.json()["peer"] is None
                finally:
                    await close_remote_speaker(app)

    asyncio.run(run())


@pytest.mark.requirement("REMOTE-SPK-8119c")
def test_wrong_code_is_403_and_fifth_wrong_code_closes_listener() -> None:
    """[if] a wrong code offers [then] 403, and the fifth closes the listener, [else stop]."""
    async def run() -> None:
        app = _engine()
        async with _client(app) as client:
            await _open_page(client)
            async with _page(client, _receiver_handler) as seen:
                state = await _start_speaker(client)
                port, code = state["port"], state["code"]
                wrong = f"{(int(code) + 1) % 1_000_000:06d}"
                try:
                    async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                        for attempt in range(1, MAX_WRONG_CODES):
                            refused = await lan.post(_offer_url(port), json={"code": wrong, "sdp": OFFER_SDP})
                            assert refused.status_code == 403
                            assert f"{MAX_WRONG_CODES - attempt} tries left" in refused.json()["detail"]
                        # Control: still open after four wrong codes, so the
                        # lockout below is the fifth miss's doing.
                        assert not _port_refuses(port)
                        last = await lan.post(_offer_url(port), json={"code": wrong, "sdp": OFFER_SDP})
                        assert last.status_code == 403
                        assert "locked out" in last.json()["detail"]
                    for _ in range(100):
                        if _port_refuses(port):
                            break
                        await asyncio.sleep(0.05)
                    assert _port_refuses(port), "the listener stayed open after the fifth wrong code"
                    after = (await client.get(f"{BASE}/speaker")).json()
                    assert after["status"] == "locked_out"
                    assert after["code"] is None
                    assert after["wrong_codes"] == MAX_WRONG_CODES
                    # No offer ever reached the page.
                    assert [c["type"] for c in seen] == ["remote_speaker_receive"]
                finally:
                    await close_remote_speaker(app)

    asyncio.run(run())


@pytest.mark.requirement("REMOTE-SPK-8119c")
def test_listener_exposes_only_the_offer_route() -> None:
    """[if] any other path is asked of the listener [then] 404, [else stop]."""
    async def run() -> None:
        session = SpeakerSession(code="123456", answer_offer=_never_answer, abandon_answer=_never_abandon)
        async with AsyncClient(
            transport=ASGITransport(app=build_listener_app(session)), base_url="http://127.0.0.1"
        ) as lan:
            # Control: the offer route IS there (a wrong code is a 403, not a 404).
            present = await lan.post(OFFER_PATH, json={"code": "000000", "sdp": OFFER_SDP})
            assert present.status_code == 403
            for method, path in (
                ("GET", "/api/v1/performance/remote-speaker/speaker"),
                ("POST", "/api/v1/commands"),
                ("GET", "/api/v1/state/ui-mirror"),
                ("GET", "/docs"),
                ("GET", "/openapi.json"),
                ("GET", "/redoc"),
                ("GET", "/"),
            ):
                response = await lan.request(method, path, json={} if method == "POST" else None)
                assert response.status_code == 404, f"{method} {path} answered {response.status_code}"
            wrong_method = await lan.get(OFFER_PATH)
            assert wrong_method.status_code == 405
            assert [getattr(r, "path", None) for r in build_listener_app(session).routes] == [OFFER_PATH]

    asyncio.run(run())


async def _never_answer(sdp: str, peer: str, epoch: str, link_id: str) -> str:
    raise AssertionError("no offer may reach the page in this test")


async def _never_abandon(link_id: str) -> None:
    raise AssertionError("no answer may be abandoned in this test")


@pytest.mark.requirement("REMOTE-SPK-8119d")
def test_stop_closes_listener_and_tells_the_page() -> None:
    """[if] the speaker stops [then] its port closes and the page is told, [else stop]."""
    async def run() -> None:
        app = _engine()
        async with _client(app) as client:
            await _open_page(client)
            async with _page(client, _receiver_handler) as seen:
                state = await _start_speaker(client)
                port = state["port"]
                assert not _port_refuses(port)
                stopped = await client.post(f"{BASE}/speaker/stop", json={})
                assert stopped.status_code == 200
                assert stopped.json()["status"] == "off"
                assert _port_refuses(port)
                assert seen[-1]["type"] == "remote_speaker_receive_stop"
                again = await client.post(f"{BASE}/speaker/stop", json={})
                assert again.status_code == 200

    asyncio.run(run())


@pytest.mark.requirement("REMOTE-SPK-8119d")
def test_teardown_stop_closes_only_its_own_session_and_orders_no_page() -> None:
    """[if] the page stops its own epoch on teardown [then] only that listener closes, [else stop]."""
    async def run() -> None:
        app = _engine()
        async with _client(app) as client:
            await _open_page(client)
            async with _page(client, _receiver_handler) as seen:
                state = await _start_speaker(client)
                port = state["port"]
                epoch = app.state.remote_speaker_session.epoch
                try:
                    other = await client.post(f"{BASE}/speaker/stop", json={"epoch": "another-session"})
                    assert other.status_code == 200
                    assert other.json()["status"] == "listening", "a newer session is left on"
                    assert not _port_refuses(port)
                    bad = await client.post(f"{BASE}/speaker/stop", json={"epoch": 7})
                    assert bad.status_code == 400
                    stopped = await client.post(f"{BASE}/speaker/stop", json={"epoch": epoch})
                    assert stopped.json()["status"] == "off"
                    assert _port_refuses(port)
                    # The leaving page dropped its own link; it is not ordered again.
                    assert [c["type"] for c in seen] == ["remote_speaker_receive"]
                    again = await client.post(f"{BASE}/speaker/start", json={"port": 0})
                    assert again.status_code == 200, "speaker mode starts normally after returning"
                finally:
                    await close_remote_speaker(app)

    asyncio.run(run())


@pytest.mark.requirement("REMOTE-SPK-8119d")
def test_stop_waits_for_an_in_flight_start() -> None:
    """[if] stop lands while a start is in flight [then] no listener is left open, [else stop]."""
    async def run() -> None:
        app = _engine()
        receiving = asyncio.Event()
        release = asyncio.Event()

        async def page(command: dict[str, Any]) -> None:
            if command["type"] == "remote_speaker_receive":
                receiving.set()
                await release.wait()

        async with _client(app) as client:
            await _open_page(client)
            async with _page(client, page) as seen:
                starting = asyncio.create_task(client.post(f"{BASE}/speaker/start", json={"port": 0}))
                await asyncio.wait_for(receiving.wait(), 5)
                stopping = asyncio.create_task(client.post(f"{BASE}/speaker/stop", json={}))
                await asyncio.sleep(0.1)
                assert not stopping.done(), "stop waits for the start it raced"
                release.set()
                started = await starting
                assert started.status_code == 200
                port = started.json()["port"]
                stopped = await stopping
                assert stopped.json()["status"] == "off"
                assert app.state.remote_speaker_session is None
                assert _port_refuses(port)
                assert [c["type"] for c in seen] == ["remote_speaker_receive", "remote_speaker_receive_stop"]

    asyncio.run(run())


@pytest.mark.requirement("REMOTE-SPK-8119")
def test_page_refusal_opens_no_port() -> None:
    """[if] the page refuses to be a speaker [then] no port opens, [else stop]."""
    def refuse(command: dict[str, Any]) -> None:
        raise RuntimeError("Remote speaker is off in this build")

    async def run() -> None:
        app = _engine()
        async with _client(app) as client:
            await _open_page(client)
            async with _page(client, refuse):
                refused = await client.post(f"{BASE}/speaker/start", json={"port": 0})
                assert refused.status_code == 400
                assert "off in this build" in refused.json()["detail"]
                assert (await client.get(f"{BASE}/speaker")).json()["status"] == "off"
                assert getattr(app.state, "remote_speaker_session", None) is None

    asyncio.run(run())


def test_routes_need_an_attached_page() -> None:
    async def run() -> None:
        async with _client(_engine()) as client:
            assert (await client.post(f"{BASE}/speaker/start", json={})).status_code == 503
            linked = await client.post(f"{BASE}/link", json={"address": "127.0.0.1:47800", "code": "123456"})
            assert linked.status_code == 503
            assert (await client.get(f"{BASE}/speaker")).json()["status"] == "off"
            assert (await client.get(f"{BASE}/link")).json()["status"] == "idle"

    asyncio.run(run())


@pytest.mark.requirement("REMOTE-SPK-8119e")
def test_sender_relays_to_a_real_second_listener_and_reads_linked() -> None:
    """[if] a sender links [then] it relays to a real listener and reads linked, [else stop]."""
    async def run() -> None:
        receiver, sender = _engine(), _engine()
        async with _client(receiver) as rx, _client(sender) as tx:
            await _open_page(rx)
            await _open_page(tx)

            async def sender_page(command: dict[str, Any]) -> None:
                if command["type"] != "remote_speaker_link":
                    return
                # The page posts its offer to its OWN engine, which relays.
                offered = await tx.post(
                    f"{BASE}/link/offer",
                    json={
                        "address": command["address"],
                        "code": command["code"],
                        "link_id": command["link_id"],
                        "sdp": OFFER_SDP,
                    },
                )
                if offered.status_code != 200:
                    raise RuntimeError(offered.json()["detail"])
                assert offered.json() == {"sdp": ANSWER_SDP}
                return

            async with _page(rx, _receiver_handler) as rx_seen, _page(tx, sender_page) as tx_seen:
                state = await _start_speaker(rx)
                try:
                    address = f"127.0.0.1:{state['port']}"
                    wrong = f"{(int(state['code']) + 1) % 1_000_000:06d}"
                    refused = await tx.post(f"{BASE}/link", json={"address": address, "code": wrong})
                    assert refused.status_code == 400
                    assert "refused the code" in refused.json()["detail"]
                    assert (await tx.get(f"{BASE}/link")).json()["status"] == "failed"

                    linked = await tx.post(f"{BASE}/link", json={"address": address, "code": state["code"]})
                    assert linked.status_code == 200, linked.text
                    assert linked.json()["status"] == "linked"
                    assert linked.json()["address"] == address
                    assert (await rx.get(f"{BASE}/speaker")).json()["status"] == "linked"
                    # The refused attempt was dropped on the page before the next one.
                    assert [c["type"] for c in tx_seen] == [
                        "remote_speaker_link",
                        "remote_speaker_unlink",
                        "remote_speaker_link",
                    ]
                    assert [c["type"] for c in rx_seen] == ["remote_speaker_receive", "remote_speaker_answer"]

                    link_id = tx_seen[-1]["link_id"]
                    stale = await tx.post(f"{BASE}/link/report", json={"state": "failed", "link_id": "older"})
                    assert stale.json()["status"] == "linked"
                    blip = await tx.post(f"{BASE}/link/report", json={"state": "disconnected", "link_id": link_id})
                    assert blip.json()["status"] == "linked", "a recoverable disconnect is not a dead link"
                    gone = await tx.post(f"{BASE}/link/report", json={"state": "failed", "link_id": link_id})
                    assert gone.json()["status"] == "disconnected"
                    unlinked = await tx.post(f"{BASE}/unlink", json={})
                    assert unlinked.json()["status"] == "idle"
                    assert tx_seen[-1]["type"] == "remote_speaker_unlink"
                finally:
                    await close_remote_speaker(receiver)

    asyncio.run(run())


def test_link_offer_is_refused_unless_a_link_is_being_made() -> None:
    async def run() -> None:
        async with _client(_engine()) as client:
            offered = await client.post(
                f"{BASE}/link/offer", json={"address": "127.0.0.1:47800", "code": "123456", "sdp": OFFER_SDP}
            )
            assert offered.status_code == 409

    asyncio.run(run())


@pytest.mark.requirement("REMOTE-SPK-8119e")
def test_relay_refuses_global_addresses() -> None:
    """[if] a link names a non-LAN address [then] the relay refuses it, [else stop]."""
    async def run() -> None:
        with pytest.raises(remote_speaker.RelayError, match="LAN only"):
            await require_lan_host("8.8.8.8")
        # Controls: the addresses a LAN speaker has are accepted.
        for host in ("127.0.0.1", "192.168.1.20", "10.0.0.5", "::1", "::ffff:192.168.1.20", "localhost"):
            await require_lan_host(host)
        for host, reason in (
            ("0.0.0.0", "unspecified"),
            ("::", "unspecified"),
            ("169.254.169.254", "metadata"),
            ("169.254.10.1", "link-local"),
            ("fe80::1", "link-local"),
            ("224.0.0.1", "multicast"),
            ("::ffff:8.8.8.8", "global"),
        ):
            with pytest.raises(remote_speaker.RelayError, match=reason):
                await require_lan_host(host)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("text", "expected"),
    [("192.168.1.20:47800", ("192.168.1.20", 47800)), ("[::1]:5", ("::1", 5)), ("dj.local:1", ("dj.local", 1))],
)
def test_parse_speaker_address_accepts(text: str, expected: tuple[str, int]) -> None:
    parsed = parse_speaker_address(text)
    assert (parsed.host, parsed.port) == expected


@pytest.mark.parametrize("text", ["", "host", ":80", "host:0", "host:65536", "::1:80", "host:abc", None])
def test_parse_speaker_address_rejects(text: object) -> None:
    with pytest.raises(ValueError):
        parse_speaker_address(text)


def test_codes_are_six_digits_and_vary() -> None:
    codes = {remote_speaker.new_code() for _ in range(50)}
    assert all(len(c) == 6 and c.isdigit() for c in codes)
    assert len(codes) > 40


# ------------------------------------------------- review fixes (PR #6221) --


async def _counting_session(port: int = 0) -> tuple[SpeakerSession, list[str]]:
    """A real listener whose page answers every offer; returns the calls it saw."""
    calls: list[str] = []

    async def answer(sdp: str, peer: str, epoch: str, link_id: str) -> str:
        calls.append(f"answer:{link_id}")
        return ANSWER_SDP

    async def abandon(link_id: str) -> None:
        calls.append(f"abandon:{link_id}")

    session = SpeakerSession(code="123456", answer_offer=answer, abandon_answer=abandon, requested_port=port)
    await session.open()
    return session, calls


@pytest.mark.requirement("REMOTE-SPK-8119c")
def test_listener_caps_the_body_before_parsing_and_runs_bounded() -> None:
    """[if] an offer body is oversized [then] the listener refuses it unparsed, [else stop]."""
    async def run() -> None:
        session, calls = await _counting_session()
        try:
            assert session._server is not None
            config = session._server.config
            assert config.limit_concurrency == remote_speaker.LISTENER_CONCURRENCY == 8
            assert config.timeout_keep_alive == remote_speaker.LISTENER_KEEP_ALIVE_S
            assert config.timeout_graceful_shutdown == remote_speaker.LISTENER_GRACEFUL_SHUTDOWN_S
            url = _offer_url(session.port or 0)
            # Not JSON: if the guard let it through, FastAPI would answer 422, not 413.
            huge = b"v" * (remote_speaker.MAX_BODY_BYTES + 1)
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                declared = await lan.post(url, content=huge, headers={"content-type": "application/json"})
                assert declared.status_code == 413

                async def trickle() -> AsyncIterator[bytes]:
                    for start in range(0, len(huge), 16 * 1024):
                        yield huge[start : start + 16 * 1024]

                streamed = await lan.post(url, content=trickle(), headers={"content-type": "application/json"})
                assert streamed.status_code == 413
                assert "content-length" not in streamed.request.headers, "precondition: really streamed"
                # Control: a normal-sized offer still links.
                ok = await lan.post(url, json={"code": "123456", "sdp": OFFER_SDP})
                assert ok.status_code == 200, ok.text
            assert session.wrong_codes == 0
            assert [c.split(":")[0] for c in calls] == ["answer"]
        finally:
            await session.close()

    asyncio.run(run())


@pytest.mark.requirement("REMOTE-SPK-8119c")
def test_listener_refuses_browsers_names_and_non_json_without_counting_a_guess() -> None:
    """[if] a browser, a name or non-JSON offers [then] it is refused uncounted, [else stop]."""
    async def run() -> None:
        session, calls = await _counting_session()
        port = session.port or 0
        url = _offer_url(port)
        body = {"code": "000000", "sdp": OFFER_SDP}
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                with_origin = await lan.post(url, json=body, headers={"origin": "http://evil.example"})
                assert with_origin.status_code == 403
                assert "Origin" in with_origin.json()["detail"]
                named = await lan.post(url, json=body, headers={"host": f"speaker.example:{port}"})
                assert named.status_code == 403
                assert "Host" in named.json()["detail"]
                plain = await lan.post(
                    url, content=b'{"code":"000000","sdp":"v=0"}', headers={"content-type": "text/plain"}
                )
                assert plain.status_code == 415
                assert session.wrong_codes == 0, "a refused request must not count toward lockout"
                # Control: the same wrong code from a plain JSON client IS counted.
                counted = await lan.post(url, json=body)
                assert counted.status_code == 403
                assert session.wrong_codes == 1
            assert calls == []
        finally:
            await session.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1:47800", True),
        ("192.168.1.20", True),
        ("[::1]:47800", True),
        ("[::1]", True),
        ("localhost:47800", False),
        ("speaker.local", False),
        ("127.0.0.1:abc", False),
        ("[::1]x", False),
        ("", False),
        (None, False),
    ],
)
def test_host_must_be_an_ip_literal(host: str | None, expected: bool) -> None:
    assert remote_speaker.host_is_ip_literal(host) is expected


@pytest.mark.requirement("REMOTE-SPK-8119b")
def test_a_linked_speaker_answers_busy_before_the_code_and_never_locks_out() -> None:
    """[if] a linked speaker gets an offer [then] 409 before the code check, [else stop]."""
    async def run() -> None:
        session, calls = await _counting_session()
        url = _offer_url(session.port or 0)
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                linked = await lan.post(url, json={"code": "123456", "sdp": OFFER_SDP})
                assert linked.status_code == 200
                for _ in range(MAX_WRONG_CODES + 2):
                    busy = await lan.post(url, json={"code": "000000", "sdp": OFFER_SDP})
                    assert busy.status_code == 409
                    assert "busy" in busy.json()["detail"]
                right = await lan.post(url, json={"code": "123456", "sdp": OFFER_SDP})
                assert right.status_code == 409, "a busy speaker must not confirm the right code"
            assert session.status == "linked"
            assert session.wrong_codes == 0
            assert not _port_refuses(session.port or 0)
            assert [c.split(":")[0] for c in calls] == ["answer"], "busy offers never reach the page"
        finally:
            await session.close()

    asyncio.run(run())


@pytest.mark.requirement("REMOTE-SPK-8119e")
def test_unlink_during_a_link_supersedes_it_and_a_late_result_is_dropped() -> None:
    """[if] unlink lands while linking [then] the late result is dropped, [else stop]."""
    async def run() -> None:
        app = _engine()
        release = asyncio.Event()

        async def slow_page(command: dict[str, Any]) -> None:
            if command["type"] == "remote_speaker_link":
                await release.wait()

        async with _client(app) as client:
            await _open_page(client)
            async with _page(client, slow_page) as seen:
                linking = asyncio.create_task(
                    client.post(f"{BASE}/link", json={"address": "127.0.0.1:47800", "code": "123456"})
                )
                for _ in range(100):
                    if seen:
                        break
                    await asyncio.sleep(0.01)
                assert (await client.get(f"{BASE}/link")).json()["status"] == "linking"
                unlinking = asyncio.create_task(client.post(f"{BASE}/unlink", json={}))
                await asyncio.sleep(0.05)
                release.set()
                late = await linking
                assert late.status_code == 409
                assert "superseded" in late.json()["detail"]
                assert (await unlinking).json()["status"] == "idle"
                assert (await client.get(f"{BASE}/link")).json()["status"] == "idle"
                assert [c["type"] for c in seen] == ["remote_speaker_link", "remote_speaker_unlink"]

    asyncio.run(run())


@pytest.mark.requirement("REMOTE-SPK-8119e")
def test_a_link_that_times_out_is_failed_and_dropped_on_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a link times out [then] it reads failed and the page drops it, [else stop]."""
    from apps.webui.server.routes import performance_remote_speaker as routes

    monkeypatch.setattr(routes, "LINK_ORDER_TIMEOUT_S", 0.3)

    async def run() -> None:
        app = _engine()

        async def stuck_page(command: dict[str, Any]) -> None:
            if command["type"] == "remote_speaker_link":
                await asyncio.sleep(0.6)

        async with _client(app) as client:
            await _open_page(client)
            async with _page(client, stuck_page) as seen:
                timed_out = await client.post(f"{BASE}/link", json={"address": "127.0.0.1:47800", "code": "123456"})
                assert timed_out.status_code == 504
                state = (await client.get(f"{BASE}/link")).json()
                assert state["status"] == "failed"
                assert [c["type"] for c in seen] == ["remote_speaker_link", "remote_speaker_unlink"]

    asyncio.run(run())


@pytest.mark.requirement("REMOTE-SPK-8119d")
def test_stop_while_answering_leaves_no_link_and_cancels_the_answer() -> None:
    """[if] stop lands while answering [then] no link is left, [else stop]."""
    async def run() -> None:
        app = _engine()
        answering = asyncio.Event()
        release = asyncio.Event()

        async def page(command: dict[str, Any]) -> dict[str, Any] | None:
            if command["type"] == "remote_speaker_answer":
                answering.set()
                await release.wait()
                return {"sdp": ANSWER_SDP}
            return None

        async with _client(app) as client:
            await _open_page(client)
            async with _page(client, page) as seen:
                state = await _start_speaker(client)
                async with httpx.AsyncClient(trust_env=False, timeout=30) as lan:
                    offer = asyncio.create_task(
                        lan.post(_offer_url(state["port"]), json={"code": state["code"], "sdp": OFFER_SDP})
                    )
                    await asyncio.wait_for(answering.wait(), 5)
                    session = app.state.remote_speaker_session
                    stopping = asyncio.create_task(client.post(f"{BASE}/speaker/stop", json={}))
                    await asyncio.sleep(0.1)
                    release.set()
                    refused = await offer
                    assert refused.status_code == 409
                    assert (await stopping).json()["status"] == "off"
                for _ in range(100):
                    if any(c["type"] == "remote_speaker_answer_cancel" for c in seen):
                        break
                    await asyncio.sleep(0.02)
                types = [c["type"] for c in seen]
                assert "remote_speaker_receive_stop" in types
                assert "remote_speaker_answer_cancel" in types
                answer_link = next(c["link_id"] for c in seen if c["type"] == "remote_speaker_answer")
                cancel_link = next(c["link_id"] for c in seen if c["type"] == "remote_speaker_answer_cancel")
                assert cancel_link == answer_link
                assert session.status == "off"
                assert session.link_id is None

    asyncio.run(run())


@pytest.mark.requirement("REMOTE-SPK-8119b")
def test_an_answer_timeout_cancels_the_pages_half_built_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] an answer times out [then] the page's half-built link is cancelled, [else stop]."""
    monkeypatch.setattr(remote_speaker, "ANSWER_TIMEOUT_S", 0.2)

    async def run() -> None:
        calls: list[str] = []

        async def slow(sdp: str, peer: str, epoch: str, link_id: str) -> str:
            await asyncio.sleep(1)
            return ANSWER_SDP

        async def abandon(link_id: str) -> None:
            calls.append(link_id)

        session = SpeakerSession(code="123456", answer_offer=slow, abandon_answer=abandon, requested_port=0)
        await session.open()
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                late = await lan.post(_offer_url(session.port or 0), json={"code": "123456", "sdp": OFFER_SDP})
            assert late.status_code == 504
            await asyncio.sleep(0.05)
            assert len(calls) == 1
            assert session.status == "listening"
        finally:
            await session.close()

    asyncio.run(run())


@pytest.mark.requirement("REMOTE-SPK-8119")
def test_concurrent_starts_open_exactly_one_listener() -> None:
    """[if] two starts race [then] exactly one listener opens, [else stop]."""
    async def run() -> None:
        app = _engine()
        async with _client(app) as client:
            await _open_page(client)
            async with _page(client, _receiver_handler) as seen:
                first, second = await asyncio.gather(
                    client.post(f"{BASE}/speaker/start", json={"port": 0}),
                    client.post(f"{BASE}/speaker/start", json={"port": 0}),
                )
                try:
                    assert sorted([first.status_code, second.status_code]) == [200, 409]
                    winner = first if first.status_code == 200 else second
                    assert not _port_refuses(winner.json()["port"])
                    assert [c["type"] for c in seen] == ["remote_speaker_receive"]
                finally:
                    await close_remote_speaker(app)

    asyncio.run(run())


@pytest.mark.requirement("REMOTE-SPK-8119")
def test_a_listener_that_cannot_open_is_a_clean_error_and_the_page_leaves_receive_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] the listener cannot open [then] a clean error, page leaves receive, [else stop]."""
    async def broken_open(self: SpeakerSession) -> None:
        raise RuntimeError("the speaker listener did not start within 2 s")

    monkeypatch.setattr(SpeakerSession, "open", broken_open)

    async def run() -> None:
        app = _engine()
        async with _client(app) as client:
            await _open_page(client)
            async with _page(client, _receiver_handler) as seen:
                failed = await client.post(f"{BASE}/speaker/start", json={"port": 0})
                assert failed.status_code == 503
                assert "did not start" in failed.json()["detail"]
                assert [c["type"] for c in seen] == ["remote_speaker_receive", "remote_speaker_receive_stop"]
                assert (await client.get(f"{BASE}/speaker")).json()["status"] == "off"

    asyncio.run(run())


class _RecordingSocket:
    def __init__(self) -> None:
        self.options: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_exclusive_bind_seam(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", -5)
    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", exclusive, raising=False)
    monkeypatch.setattr(remote_speaker.sys, "platform", platform)
    sock = _RecordingSocket()
    remote_speaker.set_exclusive_bind(sock)  # type: ignore[arg-type]
    expected = exclusive if platform == "win32" else socket.SO_REUSEADDR
    assert sock.options == [(socket.SOL_SOCKET, expected, 1)]


def test_mapped_ipv6_is_judged_as_its_ipv4_half() -> None:
    # Independent of the interpreter: before CPython 3.11.10 the mapped form
    # itself read as non-global, so the unwrap is what the guard rests on.
    import ipaddress

    assert remote_speaker.unmapped(ipaddress.ip_address("::ffff:8.8.8.8")) == ipaddress.ip_address("8.8.8.8")
    assert remote_speaker.unmapped(ipaddress.ip_address("::1")) == ipaddress.ip_address("::1")
    assert remote_speaker.lan_refusal(ipaddress.ip_address("::ffff:169.254.169.254")) is not None


def test_lan_refusal_judges_through_the_unwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """On an interpreter whose mapped addresses already delegate, only this
    shows lan_refusal judges the unwrapped address rather than the raw one."""
    import ipaddress

    monkeypatch.setattr(remote_speaker, "unmapped", lambda address: ipaddress.ip_address("8.8.8.8"))
    assert remote_speaker.lan_refusal(ipaddress.ip_address("::1")) is not None


@pytest.mark.requirement("REMOTE-SPK-8119e")
@pytest.mark.parametrize("streamed", [False, True])
def test_relay_caps_the_answer_it_reads(streamed: bool) -> None:
    """[if] an answer floods (declared or streamed) [then] the relay cuts it off, [else stop]."""
    from fastapi.responses import StreamingResponse

    async def run() -> None:
        big = FastAPI()
        flood_body = b'{"sdp": "v=0' + b"x" * (remote_speaker.MAX_BODY_BYTES + 10) + b'"}'

        @big.post(OFFER_PATH, response_model=None)
        async def flood() -> Any:
            if not streamed:
                return Response(content=flood_body, media_type="application/json")

            async def chunks() -> AsyncIterator[bytes]:
                for start in range(0, len(flood_body), 8192):
                    yield flood_body[start : start + 8192]

            return StreamingResponse(chunks(), media_type="application/json")

        server = uvicorn.Server(uvicorn.Config(big, host="127.0.0.1", port=0, log_level="warning", ws="none"))
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            with pytest.raises(remote_speaker.RelayError, match="more than"):
                await remote_speaker.relay_offer(parse_speaker_address(f"127.0.0.1:{port}"), "123456", OFFER_SDP)
        finally:
            server.should_exit = True
            await task

    asyncio.run(run())


@pytest.mark.requirement("REMOTE-SPK-8119e")
def test_relay_by_name_connects_to_the_validated_ip() -> None:
    """[if] a link names a host [then] the relay connects to the validated IP, [else stop]."""
    async def run() -> None:
        session, calls = await _counting_session()
        try:
            # The listener accepts only an IP-literal Host; a name works because
            # the relay connects to the address it validated.
            answer = await remote_speaker.relay_offer(
                parse_speaker_address(f"localhost:{session.port}"), "123456", OFFER_SDP
            )
            assert answer == ANSWER_SDP
            assert len(calls) == 1
        finally:
            await session.close()

    asyncio.run(run())


def test_close_releases_the_socket_even_when_the_server_task_failed() -> None:
    async def run() -> None:
        session, _ = await _counting_session()
        sock = session._socket
        assert sock is not None and sock.fileno() != -1

        async def boom() -> None:
            raise ValueError("server died")

        assert session._task is not None
        session._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await session._task
        session._task = asyncio.create_task(boom())
        await asyncio.sleep(0)
        with pytest.raises(ValueError, match="server died"):
            await session.close()
        assert sock.fileno() == -1

    asyncio.run(run())


def test_a_declared_oversize_body_is_refused_without_waiting_for_it() -> None:
    """Raw socket: Content-Length 1 GB and no body. The guard answers 413 at
    once; without the declared check it would wait out the 5 s body timeout."""

    async def run() -> None:
        session, calls = await _counting_session()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", session.port or 0)
            writer.write(
                (
                    f"POST {OFFER_PATH} HTTP/1.1\r\nHost: 127.0.0.1:{session.port}\r\n"
                    "Content-Type: application/json\r\nContent-Length: 1000000000\r\n\r\n"
                ).encode()
            )
            await writer.drain()
            status_line = await asyncio.wait_for(reader.readline(), 2.0)
            writer.close()
            assert b" 413 " in status_line, status_line
            assert calls == []
        finally:
            await session.close()

    asyncio.run(run())


def test_only_a_listening_session_counts_a_wrong_code() -> None:
    session = SpeakerSession(code="123456", answer_offer=_never_answer, abandon_answer=_never_abandon)
    for status in ("answering", "linked", "locked_out", "off"):
        session.status = status  # type: ignore[assignment]
        assert session.check_code("000000") is False
        assert session.check_code("123456") is False, f"a {status} session must not confirm the code"
        assert session.wrong_codes == 0
    # Control: a listening session does count.
    session.status = "listening"
    assert session.check_code("000000") is False
    assert session.wrong_codes == 1
    assert session.check_code("123456") is True


@pytest.mark.requirement("REMOTE-SPK-8119e")
def test_a_page_reported_failure_frees_the_session_and_keeps_its_reason() -> None:
    """[if] the page reports a failure and why [then] listening, reason kept, [else stop]."""

    async def run() -> None:
        app = _engine()
        async with _client(app) as client:
            await _open_page(client)
            async with _page(client, _receiver_handler) as seen:
                state = await _start_speaker(client)
                try:
                    async with httpx.AsyncClient(trust_env=False, timeout=30) as lan:
                        answered = await lan.post(
                            _offer_url(state["port"]), json={"code": state["code"], "sdp": OFFER_SDP}
                        )
                        assert answered.status_code == 200, answered.text
                    link_id = seen[-1]["link_id"]
                    bad = await client.post(
                        f"{BASE}/speaker/report", json={"state": "failed", "link_id": link_id, "error": 7}
                    )
                    assert bad.status_code == 400
                    assert (await client.get(f"{BASE}/speaker")).json()["status"] == "linked"
                    reason = "the speaker cannot start playback (NotAllowedError)"
                    reported = await client.post(
                        f"{BASE}/speaker/report", json={"state": "failed", "link_id": link_id, "error": reason}
                    )
                    body = reported.json()
                    assert body["status"] == "listening"
                    assert body["peer"] is None
                    assert reason in body["last_error"]
                finally:
                    await close_remote_speaker(app)

    asyncio.run(run())
