"""``Server-Timing: app;dur=<ms>`` on every HTTP response: the engine's own service time (PERF-DIAG).

A deck load fires several requests at one engine at once, so a client-side duration mixes queueing
with service and ``scripts/perf/amdahl_report.py`` withholds its ceiling ((private path) Q45,
option a). This pure-ASGI layer reports the server side on its own: wall time from receiving the
request to sending the response start, in the W3C Server-Timing grammar
(https://www.w3.org/TR/server-timing/), readable from ``PerformanceResourceTiming.serverTiming``
and ``Response.headers``.

It wraps ``send`` only, so a streamed body (deck audio) is never buffered: the header goes out with
``http.response.start``, which makes its duration the time to first byte.
"""

from __future__ import annotations

import time

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = "Server-Timing"
METRIC = "app"


def server_timing_value(duration_ms: float) -> str:
    """One W3C ``server-timing-metric``: ``app;dur=<ms>`` with one decimal."""
    return f"{METRIC};dur={duration_ms:.1f}"


class ServerTimingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()

        async def send_with_timing(message: Message) -> None:
            if message["type"] == "http.response.start":
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                MutableHeaders(scope=message).append(HEADER, server_timing_value(elapsed_ms))
            await send(message)

        await self.app(scope, receive, send_with_timing)
