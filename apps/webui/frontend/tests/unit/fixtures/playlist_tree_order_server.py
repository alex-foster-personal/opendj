"""Real HTTP fixture server for playlist-tree-order-route.test.mjs (LIBTREE-01/02).

Codex P1 on #6186 (discussion_r4264403126): the writer's concurrency and
rollback tests ran against a hand-written Node handler, so they could pass
while the production write path was broken. This binds a REAL uvicorn server
on an OS-assigned loopback port, serving the production ``create_app`` (the
same app tests/webui/test_playlist_tree_order.py drives) against a scratch
state.db. ``PUT /api/v1/playlist-tree/order`` is the production
``put_playlist_tree_order``: its validation, the overlay file, the
``library.changed`` publication and its 422 error mapping all run for real.

The only test hooks sit in front of the app and never answer a PUT themselves:
``/test/hold-next-put`` parks the next PUT before the route runs until
``/test/release-put``, so the JS test can overlap drags deterministically;
``/test/puts`` counts PUTs that reached the app; ``/test/shutdown`` stops it.

Usage: ``uv run --no-sync python playlist_tree_order_server.py``, then read
the ``READY <port>`` stdout line.
"""
from __future__ import annotations

import asyncio
import shutil
import signal
import socket
import sys
import tempfile
from pathlib import Path

import uvicorn
from fastapi import Request

from apps.shared.state import db as state_db
from apps.shared.state.events import EventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

_SCRATCH_DIR = Path(tempfile.mkdtemp(prefix="playlist-tree-order-server-"))
_DB_PATH = _SCRATCH_DIR / "state" / "state.db"
_ORDER_PATH = "/api/v1/playlist-tree/order"


def _seed_db(path: Path) -> None:
    path.parent.mkdir(parents=True)
    conn = state_db.open_rw(path)
    bus = EventBus()
    try:
        StateWriter(conn, bus=bus, actor="test-seed").close()
    finally:
        bus.close()
        conn.close()


def _build_app():  # noqa: ANN202 - FastAPI app from create_app
    app = create_app(
        backend=SqliteBackend(_DB_PATH), state_db_path=str(_DB_PATH),
        bind_host="127.0.0.1", hostname="test-host", mount_frontend=False,
    )
    app.state.hold_next_put = False
    app.state.put_release = None
    app.state.puts_seen = 0

    @app.middleware("http")
    async def _count_and_hold(request: Request, call_next):  # noqa: ANN001, ANN202
        if request.method == "PUT" and request.url.path == _ORDER_PATH:
            app.state.puts_seen += 1
            if app.state.hold_next_put:
                app.state.hold_next_put = False
                release = asyncio.Event()
                app.state.put_release = release
                await asyncio.wait_for(release.wait(), timeout=30.0)
        return await call_next(request)

    @app.post("/test/hold-next-put")
    def _hold() -> dict[str, bool]:
        app.state.hold_next_put = True
        return {"ok": True}

    @app.post("/test/release-put")
    def _release() -> dict[str, bool]:
        release = app.state.put_release
        app.state.put_release = None
        if release is not None:
            release.set()
        return {"released": release is not None}

    @app.get("/test/puts")
    def _puts() -> dict[str, int]:
        return {"seen": app.state.puts_seen}

    @app.post("/test/shutdown")
    def _shutdown() -> dict[str, bool]:
        app.state.server.should_exit = True
        return {"ok": True}

    return app


def _serve() -> None:
    _seed_db(_DB_PATH)
    app = _build_app()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    # listen() before READY so an instant connect queues instead of ECONNREFUSED.
    sock.listen()
    print(f"SCRATCH {_SCRATCH_DIR}", flush=True)
    print(f"READY {sock.getsockname()[1]}", flush=True)
    # sockets=, not Config(fd=...): fd reconstruction is Unix only, and the
    # Windows suite runs this fixture too (Codex P2 on #6186).
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    app.state.server = server
    server.run(sockets=[sock])


def main() -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        _serve()
    finally:
        shutil.rmtree(_SCRATCH_DIR, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
