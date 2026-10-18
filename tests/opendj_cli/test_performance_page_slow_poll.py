"""The fake performance page survives an order poll that times out.

A real local HTTP server stands in for the engine's page routes. Its first
order poll answers after the page's 5 s request timeout, which is what a
loaded runner does (two fast-lane shards, Sun 18 Oct 2026), so the page's own
thread and real requests are what the test exercises.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.opendj_cli.conftest import PerformancePage
from tests.waits import LIVENESS_CHECK_S, THREAD_HANG_GUARD_S

ORDER = {"id": "order-1", "kind": "single", "payload": {"type": "master_mute", "muted": True}}
RESULT_PATH = "/api/v1/commands/order-1/result"
SLOW_POLL_S = 6.0


@dataclass
class SlowEngine:
    """An engine whose first order poll answers too late; the next one hands out ORDER."""

    base_url: str
    polls: int = 0
    served: bool = False
    result: threading.Event = field(default_factory=threading.Event)


@pytest.fixture
def slow_engine() -> Iterator[SlowEngine]:
    state = SlowEngine(base_url="")
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass

        def _send(self, status: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _drain(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_PUT(self) -> None:
            self._drain()
            self._send(202, {})

        def do_DELETE(self) -> None:
            self._send(200, {})

        def do_GET(self) -> None:
            with lock:
                state.polls += 1
                first = state.polls == 1
            if first:
                time.sleep(SLOW_POLL_S)
                # The page already gave up on this poll, as intended.
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self._send(200, None)
                return
            with lock:
                hand_out = not state.served
                state.served = True
            self._send(200, ORDER if hand_out else None)

        def do_POST(self) -> None:
            self._drain()
            if self.path == RESULT_PATH:
                state.result.set()
            self._send(200, {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.requirement("AGENT-11")
def test_a_timed_out_order_poll_does_not_kill_the_page(slow_engine: SlowEngine) -> None:
    """[if] an order poll times out [then] the page retries and serves the order, [else stop]."""
    page = PerformancePage(slow_engine.base_url)
    page.start()
    try:
        started = time.monotonic()
        while not slow_engine.result.wait(timeout=LIVENESS_CHECK_S):
            assert page._thread is not None and page._thread.is_alive(), (
                f"DEAD: the page thread stopped after a slow poll ({page._crash!r})"
            )
            assert time.monotonic() - started < THREAD_HANG_GUARD_S, (
                f"HANG: no order result posted within {THREAD_HANG_GUARD_S}s, polls={slow_engine.polls}"
            )
        assert page._thread is not None and page._thread.is_alive()
        assert page.orders == [{"kind": "single", "payload": ORDER["payload"]}]
    finally:
        page.stop()
