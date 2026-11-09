"""PERF-DIAG-10322: every engine response carries ``Server-Timing: app;dur=<ms>``.

[if] a 50 ms handler reports a dur outside 50..150 ms [then] broken, [else stop].
[if] a streamed body is buffered, or its header is not time to first byte [then] broken, [else stop].
[if] the value does not match the W3C ``name;dur=number`` grammar [then] broken, [else stop].
[if] create_app does not install the middleware [then] broken, [else stop].
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.server_timing import HEADER, ServerTimingMiddleware, server_timing_value

pytestmark = [pytest.mark.requirement("PERF-DIAG-10322")]

#: W3C server-timing-metric: a token name, then ``;dur=`` and a number (https://www.w3.org/TR/server-timing/).
W3C_METRIC = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+;dur=\d+(\.\d+)?$")
FIRST_BYTE_SLEEP_S = 0.05
SECOND_CHUNK_GAP_S = 0.3


def _dur_ms(value: str) -> float:
    assert W3C_METRIC.match(value), value
    return float(value.split("dur=", 1)[1])


def _timed_app() -> FastAPI:
    app = FastAPI()

    @app.get("/sleep")
    async def sleep_50ms() -> dict[str, bool]:
        await asyncio.sleep(FIRST_BYTE_SLEEP_S)
        return {"ok": True}

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            yield b"first"
            await asyncio.sleep(SECOND_CHUNK_GAP_S)
            yield b"second"

        await asyncio.sleep(FIRST_BYTE_SLEEP_S)
        return StreamingResponse(chunks(), media_type="audio/mpeg")

    app.add_middleware(ServerTimingMiddleware)
    return app


def test_a_50ms_handler_reports_its_service_time() -> None:
    with TestClient(_timed_app()) as client:
        response = client.get("/sleep")
    assert response.status_code == 200
    assert 50.0 <= _dur_ms(response.headers[HEADER]) <= 150.0


def test_a_streamed_body_gets_time_to_first_byte_and_is_not_buffered() -> None:
    """Driven at the ASGI boundary: TestClient collects a stream before returning, so it cannot see buffering."""
    sent: list[tuple[float, dict[str, Any]]] = []

    requested = False

    async def receive() -> dict[str, Any]:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(SECOND_CHUNK_GAP_S * 10)  # the client stays connected for the whole stream
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append((time.perf_counter(), message))

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/stream",
        "raw_path": b"/stream",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
    }
    asyncio.run(_timed_app()(scope, receive, send))
    (start_at, start), *bodies = sent
    assert start["type"] == "http.response.start"
    headers = {key.decode(): value.decode() for key, value in start["headers"]}
    dur = _dur_ms(headers[HEADER.lower()])
    assert 50.0 <= dur < SECOND_CHUNK_GAP_S * 1000.0, "the header must go out with the first byte"
    chunks = [(at, message["body"]) for at, message in bodies if message.get("body")]
    assert b"".join(body for _, body in chunks) == b"firstsecond"
    assert len(chunks) == 2, "each chunk is its own body message, not one buffered body"
    assert chunks[1][0] - start_at >= SECOND_CHUNK_GAP_S * 0.8, "the second chunk was sent after the headers"


def test_the_value_matches_the_w3c_grammar_on_one_fixed_string() -> None:
    assert server_timing_value(50.25) == "app;dur=50.2" or server_timing_value(50.25) == "app;dur=50.3"
    assert server_timing_value(123.456) == "app;dur=123.5"
    assert W3C_METRIC.match("app;dur=123.5")
    assert not W3C_METRIC.match("app; dur=123.5")


def test_create_app_installs_the_middleware() -> None:
    app = create_app()
    assert any(middleware.cls is ServerTimingMiddleware for middleware in app.user_middleware)
