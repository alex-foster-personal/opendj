"""Regression guard for :func:`tests.waits.stop_uvicorn_in_thread`.

CloudSync acceptance tests boot real uvicorn servers on daemon threads. A
teardown that only ``join(timeout=10)`` and ignores a still-alive thread leaks
servers onto the shard; CI then times out at the pytest wall budget with the
last progress line on an unrelated module (shard 3/4, Thu 15 Oct 2026).
"""

from __future__ import annotations

import socket
import threading
import urllib.request

import pytest
import uvicorn
from fastapi import FastAPI

from tests.waits import (
    THREAD_HANG_GUARD_S,
    start_uvicorn_in_thread,
    stop_uvicorn_in_thread,
)


def test_stop_uvicorn_in_thread_waits_for_a_clean_shutdown() -> None:
    """[if] a hub thread exits on should_exit [then] stop returns, [else stop]."""
    app = FastAPI()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="waits shutdown probe")
    stop_uvicorn_in_thread(server, thread, what="waits shutdown probe")
    assert not thread.is_alive()


def test_stop_uvicorn_in_thread_raises_when_the_thread_survives() -> None:
    """[if] a real server's shutdown is held by an in-flight request [then] stop fails closed."""
    release = threading.Event()
    entered = threading.Event()
    app = FastAPI()

    @app.get("/hold")
    def _hold() -> dict[str, bool]:
        # A real in-flight request: uvicorn's graceful shutdown waits for it,
        # so should_exit alone cannot end the server thread until it returns.
        entered.set()
        return {"released": release.wait(THREAD_HANG_GUARD_S)}

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="held shutdown probe")
    client = threading.Thread(
        target=lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/hold", timeout=30).read(),
        daemon=True,
    )
    client.start()
    try:
        assert entered.wait(10.0), "the held request never reached the server"
        with pytest.raises(AssertionError, match=r"did not exit within 0\.5s \(ended by force_exit\)"):
            stop_uvicorn_in_thread(server, thread, what="held shutdown probe", join_timeout_s=0.5)
        # The request is still held: the server thread ended because stop forced
        # it, not because a release let graceful shutdown finish (Codex P1).
        assert not release.is_set() and not thread.is_alive()
    finally:
        release.set()
    # The held handler can return once released.
    thread.join(timeout=10.0)
    client.join(timeout=10.0)
    assert not thread.is_alive()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
