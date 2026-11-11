"""Engine routes for the LAN phone-remote listener (issues #10434, #10443).

``prefix="/performance/remote-control"``, mounted under ``/api/v1``. The main
engine API stays loopback-only; these routes own start, stop, status, rotate,
forget, and boot reopen of :class:`~apps.webui.server.remote_control.RemoteSession`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from apps.webui.server import remote_control
from apps.webui.server.remote_control import (
    MAX_WRONG_CODES,
    REMOTE_CONTROL_DEFAULT_PORT,
    DeviceRecord,
    RemoteSession,
    reopen_at_boot,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/performance/remote-control", tags=["performance-remote-control"])
MOUNT_UNDER_API_PREFIX = True

ControlStatusOut = Literal["off", "listening", "locked_out"]


class DeviceOut(BaseModel):
    device_id: str
    device_name: str
    paired_at: str
    last_seen: str


class RemoteControlStateOut(BaseModel):
    status: ControlStatusOut
    code: str | None = None
    address: str | None = None
    address_error: str | None = None
    port: int | None = None
    devices: list[DeviceOut] = Field(default_factory=list)
    wrong_codes: int = 0
    max_wrong_codes: int = MAX_WRONG_CODES
    last_error: str | None = None
    reopened_at_boot: bool = False


def _start_lock(app: FastAPI) -> asyncio.Lock:
    lock = getattr(app.state, "remote_control_start_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.remote_control_start_lock = lock
    return lock


def _session(app: FastAPI) -> RemoteSession | None:
    return getattr(app.state, "remote_control_session", None)


def state_dir_for(app: FastAPI) -> Path:
    """``<data>/state``: the directory that holds ``remote-devices.json``."""
    return Path(app.state.state_db_path).resolve().parent


def _device_out(record: DeviceRecord) -> DeviceOut:
    return DeviceOut(
        device_id=record.device_id,
        device_name=record.device_name,
        paired_at=record.paired_at,
        last_seen=record.last_seen,
    )


def _devices_from_disk(state_dir: Path) -> list[DeviceOut]:
    path = state_dir / remote_control.REMOTE_DEVICES_FILE
    if not path.exists():
        return []
    stub = RemoteSession(state_dir=state_dir, status="off")
    return [_device_out(row) for row in stub._load_devices().values()]


def _state_out(app: FastAPI) -> RemoteControlStateOut:
    session = _session(app)
    if session is None:
        return RemoteControlStateOut(status="off", devices=_devices_from_disk(state_dir_for(app)))
    devices = [_device_out(row) for row in session._load_devices().values()]
    if session.is_open:
        status: ControlStatusOut = "locked_out" if session.status == "locked_out" else "listening"
        return RemoteControlStateOut(
            status=status,
            code=None if status == "locked_out" else session.display_code,
            address=session.address,
            address_error=session.address_error,
            port=session.port,
            devices=devices,
            wrong_codes=session.wrong_codes,
            max_wrong_codes=MAX_WRONG_CODES,
            last_error=session.last_error,
            reopened_at_boot=bool(session.reopened_at_boot),
        )
    return RemoteControlStateOut(
        status=session.status,
        devices=devices,
        last_error=session.last_error,
        wrong_codes=session.wrong_codes if session.status == "locked_out" else 0,
    )


def _validate_port(value: object) -> int:
    if value is None:
        return REMOTE_CONTROL_DEFAULT_PORT
    if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= 65535):
        raise HTTPException(status_code=400, detail=f"port must be an integer 0..65535, got {value!r}")
    return value


async def close_remote_control(app: FastAPI, *, clear_listening: bool = False) -> None:
    """Close the LAN listener. Engine shutdown keeps the saved listening port."""
    async with _start_lock(app):
        session = _session(app)
        if session is None:
            if clear_listening:
                _clear_saved_listening(app, RemoteSession(state_dir=state_dir_for(app)))
            return
        if session.is_open:
            await session.close(clear_listening=clear_listening)
        elif clear_listening:
            # Session missing its listener (closed, or reopened-at-boot and failed):
            # an explicit stop must still drop the saved reopen hint.
            session.port = None
            _clear_saved_listening(app, session)
        if clear_listening or session.status == "off":
            app.state.remote_control_session = None
        elif not session.is_open and session.status != "locked_out":
            # Shutdown path: drop the live handle; listening stays on disk.
            app.state.remote_control_session = None


def _clear_saved_listening(app: FastAPI, session: RemoteSession) -> None:
    """Write ``listening: null`` when a port is saved; never raise on a bad file."""
    try:
        if remote_control.read_listening_port(state_dir_for(app)) is None:
            return
        session.write_listening(None)
    except (OSError, ValueError):
        logger.warning("remote control: could not clear saved listening port", exc_info=True)


def engine_url_for(app: FastAPI, request: Request | None = None) -> str:
    """Loopback origin of this engine, for the phone listener's public request routes.

    The engine's own port is ``app.state.port`` (set at boot); a live request's ASGI
    ``server`` scope is the fallback. With neither, the default origin (port 80) stays.
    """
    port = getattr(app.state, "port", None)
    if port is None and request is not None:
        server = request.scope.get("server")
        port = server[1] if server else None
    if not isinstance(port, int) or isinstance(port, bool) or not (0 < port <= 65535):
        return RemoteSession.engine_url
    return f"http://127.0.0.1:{port}"


async def restore_remote_control(app: FastAPI) -> None:
    """Boot: reopen the listener when ``listening.port`` was saved at quit."""
    async with _start_lock(app):
        if _session(app) is not None:
            return
        try:
            session = await reopen_at_boot(state_dir_for(app), engine_url=engine_url_for(app))
        except (OSError, ValueError, RuntimeError, TypeError):
            # Malformed or unreadable remote-devices.json (bad JSON, devices null, a row
            # missing fields) must not abort engine boot. open() already closed any
            # listener it bound before write_listening failed.
            logger.warning("remote control: could not restore listener at boot", exc_info=True)
            return
        if session is not None:
            app.state.remote_control_session = session


@router.get("", response_model=RemoteControlStateOut)
async def get_remote_control(request: Request) -> RemoteControlStateOut:
    """Status, code, address, paired devices. Never the token or its hash."""
    return _state_out(request.app)


@router.post("/start", response_model=RemoteControlStateOut)
async def post_remote_control_start(
    request: Request,
    body: Annotated[dict[str, Any] | None, Body(title="RemoteControlStartBody")] = None,
) -> RemoteControlStateOut:
    """Open the LAN listener. Idempotent while already listening."""
    port = _validate_port((body or {}).get("port"))
    app = request.app
    async with _start_lock(app):
        existing = _session(app)
        if existing is not None and existing.is_open:
            return _state_out(app)
        if existing is not None:
            app.state.remote_control_session = None
        session = RemoteSession(
            state_dir=state_dir_for(app), requested_port=port, engine_url=engine_url_for(app, request)
        )
        try:
            await session.open()
        except OSError as error:
            raise HTTPException(
                status_code=409,
                detail=f"the remote-control listener could not open on port {port}: {error}",
            ) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        app.state.remote_control_session = session
        return _state_out(app)


@router.post("/stop", response_model=RemoteControlStateOut)
async def post_remote_control_stop(request: Request) -> RemoteControlStateOut:
    """Close the listener. Paired devices survive. Idempotent."""
    await close_remote_control(request.app, clear_listening=True)
    return _state_out(request.app)


@router.post("/rotate-code", response_model=RemoteControlStateOut)
async def post_remote_control_rotate(request: Request) -> RemoteControlStateOut:
    """Issue a new pairing code without stopping."""
    session = _session(request.app)
    if session is None or not session.is_open:
        raise HTTPException(status_code=409, detail="remote-control listener is not listening")
    session.rotate_code()
    return _state_out(request.app)


@router.delete("/devices/{device_id}", response_model=RemoteControlStateOut)
async def delete_remote_control_device(device_id: str, request: Request) -> RemoteControlStateOut:
    """Forget one paired device (404 when unknown)."""
    state_dir = state_dir_for(request.app)
    session = _session(request.app)
    if session is None:
        session = RemoteSession(state_dir=state_dir, status="off")
    devices = session._load_devices()
    if device_id not in devices:
        raise HTTPException(status_code=404, detail=f"unknown device_id {device_id}")
    session.forget(device_id)
    return _state_out(request.app)
