"""REMOTE-CTL restart: paired phones survive an engine restart (issue #10443).

[if] the listener was on at shutdown [then] the new app listens on the same port with a new code, [else stop].
[if] a phone paired before the restart pings after it [then] the ping returns 200, [else stop].
[if] the listener was off at shutdown [then] the new app does not open the port, [else stop].
[if] the port is taken at boot [then] status is off with last_error and boot completes, [else stop].
[if] the saved port is valid but the device table is malformed at boot [then] boot completes with no listener, [else stop].
[if] the engine runs on a non-80 port [then] the phone listener reaches it there from /start and at boot, [else stop].
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from apps.webui.server.remote_control import (
    PAIR_PATH,
    PING_PATH,
    REMOTE_DEVICES_FILE,
    RemoteSession,
    parse_listening,
    read_listening_port,
)
from apps.webui.server.routes import performance_remote_control as remote_routes
from apps.webui.server.routes.performance_remote_control import router as remote_router

pytestmark = pytest.mark.requirement("REMOTE-CTL-10713")

BASE = "/api/v1/performance/remote-control"


def _port_refuses(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", port)) != 0


@asynccontextmanager
async def _engine(state_dir: Path, engine_port: int | None = None) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    state_dir.mkdir(parents=True, exist_ok=True)
    db = state_dir / "state.db"
    db.touch()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if engine_port is not None:
            app.state.port = engine_port
        await remote_routes.restore_remote_control(app)
        try:
            yield
        finally:
            await remote_routes.close_remote_control(app)

    app = FastAPI(lifespan=lifespan)
    app.state.state_db_path = str(db)
    app.include_router(remote_router, prefix="/api/v1")
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://engine", timeout=30) as client,
    ):
        yield app, client


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


def test_listener_reopens_at_boot_with_fresh_code_and_paired_ping(state_dir: Path) -> None:
    """[if] listening at quit [then] boot reopens the port with a new code and ping is 200, [else stop]."""

    async def run() -> None:
        token: str | None = None
        port: int | None = None
        old_code: str | None = None
        async with _engine(state_dir) as (_app, client):
            started = await client.post(f"{BASE}/start", json={"port": 0})
            assert started.status_code == 200, started.text
            body = started.json()
            assert body["status"] == "listening"
            port = int(body["port"])
            old_code = body["code"]
            assert old_code
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                paired = await lan.post(
                    f"http://127.0.0.1:{port}{PAIR_PATH}",
                    json={"code": old_code, "device_name": "the maintainer phone"},
                    headers={"host": f"127.0.0.1:{port}"},
                )
                assert paired.status_code == 201, paired.text
                token = paired.json()["token"]
            # Engine "quit": lifespan closes without clear_listening.
        assert port is not None and token is not None and old_code is not None
        on_disk = json.loads((state_dir / "remote-devices.json").read_text(encoding="utf-8"))
        assert on_disk.get("listening") == {"port": port}

        async with _engine(state_dir) as (_app2, client2):
            status = await client2.get(BASE)
            assert status.status_code == 200, status.text
            body2 = status.json()
            assert body2["status"] == "listening"
            assert body2["port"] == port
            assert body2["reopened_at_boot"] is True
            assert body2["code"] and body2["code"] != old_code
            assert not _port_refuses(port)
            async with httpx.AsyncClient(trust_env=False, timeout=10) as lan:
                ping = await lan.get(
                    f"http://127.0.0.1:{port}{PING_PATH}",
                    headers={"host": f"127.0.0.1:{port}", "authorization": f"Bearer {token}"},
                )
                assert ping.status_code == 200, ping.text
                assert ping.json()["device_name"] == "the maintainer phone"

    asyncio.run(run())


def test_listener_off_at_quit_does_not_reopen(state_dir: Path) -> None:
    """[if] the listener was off at shutdown [then] the new app does not open the port, [else stop]."""

    async def run() -> None:
        port: int | None = None
        async with _engine(state_dir) as (_app, client):
            started = await client.post(f"{BASE}/start", json={"port": 0})
            assert started.status_code == 200, started.text
            port = int(started.json()["port"])
            stopped = await client.post(f"{BASE}/stop", json={})
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["status"] == "off"
        assert port is not None
        on_disk = json.loads((state_dir / "remote-devices.json").read_text(encoding="utf-8"))
        assert on_disk.get("listening") is None

        async with _engine(state_dir) as (_app2, client2):
            status = await client2.get(BASE)
            assert status.status_code == 200, status.text
            body = status.json()
            assert body["status"] == "off"
            assert body["reopened_at_boot"] is False
            assert body["port"] is None
            assert _port_refuses(port)

    asyncio.run(run())


def test_busy_port_at_boot_leaves_off_with_last_error(state_dir: Path) -> None:
    """[if] the port is taken at boot [then] status is off with last_error, [else stop]."""

    async def run() -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("0.0.0.0", 0))
        holder.listen(1)
        busy_port = int(holder.getsockname()[1])
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            # Seed a listening hint on the busy port with no live session.
            seed = RemoteSession(state_dir=state_dir, status="off")
            seed.write_listening(busy_port)
            async with _engine(state_dir) as (_app, client):
                status = await client.get(BASE)
                assert status.status_code == 200, status.text
                body = status.json()
                assert body["status"] == "off"
                assert body["reopened_at_boot"] is False
                assert body["last_error"]
                assert str(busy_port) in body["last_error"]
                assert "boot" in body["last_error"]
        finally:
            holder.close()

    asyncio.run(run())


def test_listening_parsers_share_one_port_range(state_dir: Path) -> None:
    """[if] listening.port is 0 or outside 1..65535 [then] peek and boot both treat it as absent, [else stop].
    [if] listening.port is in 1..65535 [then] peek and boot both return that port, [else stop].
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / REMOTE_DEVICES_FILE

    def write(raw: object) -> None:
        path.write_text(json.dumps({"schema": 1, "listening": raw, "devices": {}}), encoding="utf-8")

    write({"port": 0})
    session = RemoteSession(state_dir=state_dir, status="off")
    assert session._peek_listening() is None
    assert read_listening_port(state_dir) is None
    assert parse_listening({"port": 0}) is None

    write({"port": 12345})
    session = RemoteSession(state_dir=state_dir, status="off")
    assert session._peek_listening() == {"port": 12345}
    assert read_listening_port(state_dir) == 12345
    assert parse_listening({"port": 12345}) == {"port": 12345}


def test_stop_clears_saved_port_when_busy_boot_left_session_off(state_dir: Path) -> None:
    """[if] boot reopen failed on a busy port and the user stops [then] the saved port is cleared, [else stop]."""

    async def run() -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("0.0.0.0", 0))
        holder.listen(1)
        busy_port = int(holder.getsockname()[1])
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            RemoteSession(state_dir=state_dir, status="off").write_listening(busy_port)
            async with _engine(state_dir) as (_app, client):
                assert read_listening_port(state_dir) == busy_port
                stopped = await client.post(f"{BASE}/stop")
                assert stopped.status_code == 200, stopped.text
                assert read_listening_port(state_dir) is None
        finally:
            holder.close()

    asyncio.run(run())


def test_stop_clears_saved_port_when_no_session(state_dir: Path) -> None:
    """[if] no session exists but a port is saved and the user stops [then] the saved port is cleared, [else stop]."""

    async def run() -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        RemoteSession(state_dir=state_dir, status="off").write_listening(45678)
        db = state_dir / "state.db"
        db.touch()
        app = FastAPI()
        app.state.state_db_path = str(db)
        app.include_router(remote_router, prefix="/api/v1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://engine") as client:
            stopped = await client.post(f"{BASE}/stop")
            assert stopped.status_code == 200, stopped.text
        assert read_listening_port(state_dir) is None

    asyncio.run(run())


def test_malformed_devices_file_does_not_abort_boot(state_dir: Path) -> None:
    """[if] remote-devices.json is malformed at boot [then] restore logs and boot completes, [else stop]."""

    async def run() -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / REMOTE_DEVICES_FILE).write_text("{not json", encoding="utf-8")
        async with _engine(state_dir) as (app, _client):
            assert getattr(app.state, "remote_control_session", None) is None

    asyncio.run(run())


def test_open_closes_listener_when_write_listening_fails(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] persisting the port fails in open [then] the listener is closed and the error re-raised, [else stop]."""

    async def run() -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        session = RemoteSession(state_dir=state_dir, requested_port=0)

        def boom(_port: int | None) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(session, "write_listening", boom)
        with pytest.raises(OSError, match="disk full"):
            await session.open()
        assert session._task is None
        assert session._socket is None
        assert not session.is_open

    asyncio.run(run())


def test_close_still_stops_listener_when_clearing_saved_port_fails(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] clearing the saved port fails on close [then] the listener is still down and close returns, [else stop]."""

    async def run() -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        session = RemoteSession(state_dir=state_dir, requested_port=0)
        await session.open()
        port = int(session.port or 0)

        def boom(_port: int | None) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(session, "write_listening", boom)
        await session.close(clear_listening=True)
        assert not session.is_open
        assert session.port is None
        assert _port_refuses(port)

    asyncio.run(run())


@pytest.mark.parametrize(
    "devices",
    [None, {"d1": {"name": "phone"}}],
    ids=["devices-null", "row-missing-fields"],
)
def test_malformed_device_table_with_valid_port_does_not_abort_boot(state_dir: Path, devices: object) -> None:
    """[if] the port is valid but the device table is malformed [then] boot completes and the listener is closed, [else stop]."""

    async def run() -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        port = socket.socket()
        port.bind(("127.0.0.1", 0))
        free = port.getsockname()[1]
        port.close()
        doc = {"devices": devices, "listening": {"port": free}}
        (state_dir / REMOTE_DEVICES_FILE).write_text(json.dumps(doc), encoding="utf-8")
        async with _engine(state_dir) as (app, _client):
            assert getattr(app.state, "remote_control_session", None) is None
            assert _port_refuses(free)

    asyncio.run(run())


def test_listener_engine_origin_follows_engine_port_on_start_and_at_boot(state_dir: Path) -> None:
    """[if] the engine port is not 80 [then] /start and boot reopen set that loopback origin, [else stop]."""

    async def run() -> None:
        async with _engine(state_dir) as (app, client):
            app.state.port = 8123
            started = await client.post(f"{BASE}/start", json={"port": 0})
            assert started.status_code == 200, started.text
            assert app.state.remote_control_session.engine_url == "http://127.0.0.1:8123"
        async with _engine(state_dir, engine_port=8456) as (app2, _client2):
            assert app2.state.remote_control_session.engine_url == "http://127.0.0.1:8456"

    asyncio.run(run())


def test_engine_origin_falls_back_to_request_server_then_default() -> None:
    """[if] app.state.port is unset [then] the request's server port is used, else the default origin, [else stop]."""
    app = FastAPI()

    with_server = Request({"type": "http", "server": ("127.0.0.1", 9001)})
    without_server = Request({"type": "http", "server": None})

    assert remote_routes.engine_url_for(app, with_server) == "http://127.0.0.1:9001"
    assert remote_routes.engine_url_for(app, without_server) == RemoteSession.engine_url
    assert remote_routes.engine_url_for(app) == RemoteSession.engine_url


@pytest.mark.requirement("LIBM-13766")
def test_device_table_a_newer_build_wrote_still_loads(state_dir: Path) -> None:
    """[if] a newer build stored an extra per-device key [then] the device still loads with its known fields, [else stop]."""
    state_dir.mkdir(parents=True, exist_ok=True)
    row = {
        "device_id": "d1",
        "device_name": "phone",
        "token_sha256": "ab" * 32,
        "paired_at": "2026-11-12T10:00:00+00:00",
        "last_seen": "2026-11-12T10:00:00+00:00",
        "key_added_by_a_newer_build": True,
    }
    (state_dir / REMOTE_DEVICES_FILE).write_text(
        json.dumps({"schema": 1, "listening": None, "devices": {"d1": row}}), encoding="utf-8"
    )
    devices = RemoteSession(state_dir=state_dir, status="off")._load_devices()
    assert devices["d1"].device_name == "phone"
