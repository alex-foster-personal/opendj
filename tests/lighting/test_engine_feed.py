"""LIGHT-05 engine feed adapter against real loopback websocket servers (engine + daemon)."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from apps.lighting.engine_feed import (
    EngineEndpoint,
    EngineFeed,
    EngineFeedAuthError,
    EngineFeedDisconnected,
    EngineFeedError,
    FeedBatch,
    parse_state_line,
    refetch_ids,
)

FIXTURE = Path(__file__).parent / "fixtures" / "engine_state_lines.ndjson"
LINES = [json.loads(s) for s in FIXTURE.read_text().splitlines()]
TOKEN = "good-token"


def _beats(offset_s: float) -> list[dict[str, Any]]:
    return [{"n": i + 1, "t": round(offset_s + i * 0.5, 3), "bpm": 120.0} for i in range(8)]


def _grid_ms(offset_s: float) -> tuple[dict[str, float | int], ...]:
    return tuple({"n": b["n"], "time_ms": b["t"] * 1000.0, "bpm": 120.0} for b in _beats(offset_s))


@dataclass
class Rig:
    mirror: dict[str, str | None] = field(default_factory=lambda: {"1": "trk-a", "2": "trk-b0"})
    mirror_status: int = HTTPStatus.OK
    grid_offsets: dict[str, float] = field(default_factory=lambda: {"trk-a": 0.1, "trk-b0": 0.2, "trk-b1": 0.3})
    fetches: Counter[str] = field(default_factory=Counter)
    handshakes: int = 0
    sent: int = 0
    on_sent: Callable[[Rig, int], None] = lambda rig, n: None
    events: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)


# ----------------------------------------------------------------------------- servers


def _json_response(status: int, body: Any) -> Response:
    data = json.dumps(body).encode()
    headers = Headers({"Content-Type": "application/json", "Content-Length": str(len(data))})
    return Response(status, HTTPStatus(status).phrase, headers, data)


def _engine_server(rig: Rig) -> Awaitable[Server]:
    def check(conn: ServerConnection, req: Request) -> Response | None:
        rig.handshakes += 1
        url = urlsplit(req.path)
        if url.path != "/api/v1/ws":
            return conn.respond(HTTPStatus.NOT_FOUND, "not found\n")
        if parse_qs(url.query).get("token") != [TOKEN]:
            return conn.respond(HTTPStatus.UNAUTHORIZED, "missing or wrong token\n")
        return None

    async def handler(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "hello", "protocol": 1}))
        for line in LINES:
            await ws.send(json.dumps(line))
            rig.sent += 1
            rig.on_sent(rig, rig.sent)
            await asyncio.sleep(1 / 30)
        await ws.close(1000, "done")

    return serve(handler, "127.0.0.1", 0, process_request=check)


def _daemon_server(rig: Rig) -> Awaitable[Server]:
    def route(conn: ServerConnection, req: Request) -> Response | None:
        path = urlsplit(req.path).path
        if path == "/api/v1/events":
            return None
        if path == "/api/v1/state/ui-mirror":
            if rig.mirror_status != HTTPStatus.OK:
                return _json_response(rig.mirror_status, {"client_open": False})
            return _json_response(HTTPStatus.OK, {"decks": {k: {"stable_id": v} for k, v in rig.mirror.items()}})
        if path.startswith("/api/v1/tracks/") and path.endswith("/anlz"):
            tid = path.split("/")[4]
            rig.fetches[tid] += 1
            return _json_response(
                HTTPStatus.OK, {"beatgrid": {"source": "rekordbox", "beats": _beats(rig.grid_offsets[tid])}}
            )
        return _json_response(HTTPStatus.NOT_FOUND, {})

    async def events(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "hello"}))
        closed = asyncio.ensure_future(ws.wait_closed())
        while True:
            get = asyncio.ensure_future(rig.events.get())
            await asyncio.wait({get, closed}, return_when=asyncio.FIRST_COMPLETED)
            if closed.done():
                get.cancel()
                return
            await ws.send(json.dumps(get.result()))

    return serve(events, "127.0.0.1", 0, process_request=route)


def _port(server: Server) -> int:
    return next(iter(server.sockets)).getsockname()[1]


async def _run(
    rig: Rig, token: str = TOKEN, after_batch: Callable[[Rig, int], None] | None = None
) -> tuple[list[FeedBatch], EngineFeed]:
    async with await _engine_server(rig) as engine, await _daemon_server(rig) as daemon:
        dport = _port(daemon)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{dport}") as http:
            feed = EngineFeed(
                http=http,
                endpoint=EngineEndpoint(f"ws://127.0.0.1:{_port(engine)}/api/v1/ws", token),
                events_url=f"ws://127.0.0.1:{dport}/api/v1/events",
                mirror_poll_s=0.0,
            )
            got: list[FeedBatch] = []
            with pytest.raises(EngineFeedDisconnected):
                async for batch in feed.batches():
                    got.append(batch)
                    if after_batch:
                        after_batch(rig, len(got))
            return got, feed


# ----------------------------------------------------------------------------- acceptance


def test_replays_30_lines_into_30_batches_with_both_decks() -> None:
    msg = (
        "if the loopback server replays 30 state lines with two playing decks "
        "then the adapter yields 30 sample batches with both decks and correct positions"
    )
    batches, _ = asyncio.run(_run(Rig()))
    assert len(batches) == 30, msg
    for batch, line in zip(batches, LINES, strict=True):
        assert [d.sample.deck for d in batch.decks] == ["1", "2"], msg
        for frame, src in zip(batch.decks, line["decks"][:2], strict=True):
            assert frame.sample.position_ms == src["position_ms"], msg
            assert frame.sample.rate == src["rate"] and frame.sample.playing, msg
            assert frame.sample.host_time_ns - batch.received_ns == 12_000_000, msg
        assert batch.mixer.faders == {"1": 1.0, "2": 0.8}, msg
        assert batch.mixer.master_rms == line["master"]["rms"], msg


def test_new_track_on_deck_2_fetches_anlz_once_and_attaches_grid() -> None:
    msg = "if a new track id appears on deck 2 then exactly one /anlz fetch happens for it and its grid is attached to later samples"

    def flip(rig: Rig, n: int) -> None:
        if n == 10:
            rig.mirror["2"] = "trk-b1"

    rig = Rig(on_sent=flip)
    batches, feed = asyncio.run(_run(rig))
    assert feed.anlz_fetches["trk-b1"] == 1 and rig.fetches["trk-b1"] == 1, msg
    assert feed.anlz_fetches["trk-a"] == 1, msg
    ids = [b.decks[1].track_id for b in batches]
    first = ids.index("trk-b1")
    assert set(ids[:first]) == {"trk-b0"} and set(ids[first:]) == {"trk-b1"}, msg
    analysis = batches[-1].decks[1].analysis
    assert analysis is not None and analysis.grid == _grid_ms(0.3), msg


def test_wrong_token_raises_auth_error_without_retry() -> None:
    msg = "if the token is wrong and the server closes with an auth error then the adapter raises EngineFeedAuthError, not a silent retry"
    rig = Rig()

    async def go() -> None:
        async with await _engine_server(rig) as engine, httpx.AsyncClient(base_url="http://127.0.0.1:9") as http:
            feed = EngineFeed(
                http=http,
                endpoint=EngineEndpoint(f"ws://127.0.0.1:{_port(engine)}/api/v1/ws", "wrong"),
                events_url="ws://127.0.0.1:9/api/v1/events",
            )
            with pytest.raises(EngineFeedAuthError):
                await anext(feed.batches())

    asyncio.run(go())
    assert rig.handshakes == 1, msg


def test_beatgrid_edit_for_loaded_track_refetches_grid_once() -> None:
    msg = (
        "if a library.changed event with kind beatgrid_edit for a loaded track arrives then its grid is refetched once"
    )

    def edit(rig: Rig, n: int) -> None:
        if n == 5:
            rig.grid_offsets["trk-a"] = 0.9
            env = {
                "topic": "library.changed",
                "seq": 1,
                "ts": "t",
                "payload": {"kind": "beatgrid_edit", "ids": ["trk-a", "trk-not-loaded"]},
            }
            rig.events.put_nowait(env)

    batches, feed = asyncio.run(_run(Rig(), after_batch=edit))
    assert feed.anlz_fetches["trk-a"] == 2, msg
    assert "trk-not-loaded" not in feed.anlz_fetches, msg
    analysis = batches[-1].decks[0].analysis
    assert analysis is not None and analysis.grid == _grid_ms(0.9), msg


# ----------------------------------------------------------------------------- helpers and edges


def test_refetch_ids_only_for_beatgrid_and_sections_kinds() -> None:
    env = {"topic": "library.changed", "payload": {"kind": "sections", "ids": ["x"]}}
    assert refetch_ids(env) == {"x"}, "if a sections kind arrives then it is refetched"
    assert refetch_ids({**env, "payload": {"kind": "hot_cues", "ids": ["x"]}}) == set(), (
        "if hot_cues arrives then nothing refetches"
    )
    assert refetch_ids({**env, "topic": "deck.loaded"}) == set(), "if another topic arrives then nothing refetches"


def test_mirror_409_leaves_track_ids_unknown_without_anlz_fetch() -> None:
    msg = "if no page is open (ui-mirror 409) then frames carry no track id and no anlz is fetched"
    batches, feed = asyncio.run(_run(Rig(mirror_status=HTTPStatus.CONFLICT)))
    assert len(batches) == 30, msg
    assert all(d.track_id is None and d.analysis is None for b in batches for d in b.decks), msg
    assert not feed.anlz_fetches, msg


def test_parse_state_line_null_heard_in_and_missing_key() -> None:
    line = {**LINES[0], "heard_in_ns": None}
    batch = parse_state_line(line, 777, {}, {})
    assert batch.decks[0].sample.host_time_ns == 777, "if heard_in_ns is null then host time is the receipt time"
    bad_deck = {k: v for k, v in LINES[0]["decks"][0].items() if k != "position_ms"}
    with pytest.raises(EngineFeedError, match="position_ms"):
        parse_state_line({**LINES[0], "decks": [bad_deck]}, 0, {}, {})
