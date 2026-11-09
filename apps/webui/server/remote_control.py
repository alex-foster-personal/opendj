"""Remote control listener (issue #10432): LAN pairing for a phone controller.

Threat model (same as REMOTE-SPK): plain HTTP on the LAN, no TLS. A passive LAN
sniffer can read a token and act as that phone until Forget. Accepted for a
LAN-only feature whose scope is a bounded command allowlist (later child);
never a path to the full API, ``state.db`` or the share tunnel. The code grants
remote scope only and is useless off the LAN (private, loopback and CGNAT source
addresses only, reusing the speaker's address rule).

The main engine API stays loopback-only. This module owns a separate FastAPI app
on ``0.0.0.0`` exposing ``POST /remote/pair`` and ``GET /remote/ping`` only.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
from collections.abc import Coroutine
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field

from apps.b2b_pairing.b2b_pairing import PairingError, generate_code, normalize_code
from apps.shared.private_files import is_owner_only, restrict_to_owner
from apps.webui.server.remote_speaker import (
    LISTENER_CONCURRENCY,
    LISTENER_GRACEFUL_SHUTDOWN_S,
    LISTENER_KEEP_ALIVE_S,
    ListenerGuard,
    _QuietServer,
    lan_address,
    set_exclusive_bind,
)

logger = logging.getLogger(__name__)

REMOTE_CONTROL_BIND_HOST = "0.0.0.0"
REMOTE_CONTROL_DEFAULT_PORT = 47810
PAIR_PATH = "/remote/pair"
PING_PATH = "/remote/ping"
REMOTE_DEVICES_FILE = "remote-devices.json"
MAX_WRONG_CODES = 5
MAX_BODY_BYTES = 16 * 1024
DEVICE_NAME_LEN = (1, 40)

ControlStatus = Literal["listening", "locked_out"]


class PairBody(BaseModel):
    code: str
    device_name: str = Field(min_length=DEVICE_NAME_LEN[0], max_length=DEVICE_NAME_LEN[1])


class PairOut(BaseModel):
    device_id: str
    device_name: str
    token: str


class PingOut(BaseModel):
    device_id: str
    device_name: str


@dataclass(frozen=True)
class DeviceRecord:
    device_id: str
    device_name: str
    token_sha256: str
    paired_at: str
    last_seen: str


@dataclass
class RemoteSession:
    """One remote-control listener: Crockford code, device table, wrong-code budget."""

    state_dir: Path
    bind_host: str = REMOTE_CONTROL_BIND_HOST
    requested_port: int = REMOTE_CONTROL_DEFAULT_PORT
    status: ControlStatus = "listening"
    port: int | None = None
    address: str | None = None
    address_error: str | None = None
    wrong_codes: int = 0
    display_code: str = field(default_factory=generate_code)
    _canonical_code: str = field(default="", repr=False)
    _server: _QuietServer | None = None
    _task: asyncio.Task[None] | None = None
    _socket: socket.socket | None = None
    _background: set[asyncio.Task[None]] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not self._canonical_code:
            self._canonical_code = normalize_code(self.display_code)

    @property
    def devices_path(self) -> Path:
        return self.state_dir / REMOTE_DEVICES_FILE

    def _load_devices(self) -> dict[str, DeviceRecord]:
        path = self.devices_path
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("devices")
        if not isinstance(raw, dict):
            raise RuntimeError(f"{path} is not a device table")
        out: dict[str, DeviceRecord] = {}
        for key, row in raw.items():
            if not isinstance(row, dict):
                continue
            out[key] = DeviceRecord(**row)
        return out

    def _save_devices(self, devices: dict[str, DeviceRecord]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.devices_path
        tmp = path.with_suffix(".tmp")
        payload = {"schema": 1, "devices": {k: asdict(v) for k, v in sorted(devices.items())}}
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        restrict_to_owner(tmp)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        restrict_to_owner(path)

    def forget(self, device_id: str) -> None:
        devices = self._load_devices()
        if device_id in devices:
            del devices[device_id]
            self._save_devices(devices)

    def _rotate_code(self) -> None:
        self.display_code = generate_code()
        self._canonical_code = normalize_code(self.display_code)

    def check_code(self, typed: str) -> bool:
        if self.status != "listening":
            return False
        try:
            normalized = normalize_code(typed)
        except PairingError:
            normalized = ""
        if hmac.compare_digest(normalized, self._canonical_code):
            return True
        self.wrong_codes += 1
        if self.wrong_codes >= MAX_WRONG_CODES:
            self.status = "locked_out"
            logger.warning("remote control: %s wrong codes; listener closed", MAX_WRONG_CODES)
            self.close_soon()
        return False

    def pair_device(self, device_name: str) -> tuple[DeviceRecord, str]:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(UTC).isoformat()
        device_id = secrets.token_hex(8)
        record = DeviceRecord(
            device_id=device_id,
            device_name=device_name,
            token_sha256=digest,
            paired_at=now,
            last_seen=now,
        )
        devices = self._load_devices()
        devices[device_id] = record
        self._save_devices(devices)
        self._rotate_code()
        return record, token

    def authenticate(self, token: str | None) -> DeviceRecord:
        if token is None or token == "":
            raise HTTPException(status_code=401, detail="missing bearer token")
        digest = hashlib.sha256(token.encode()).hexdigest()
        devices = self._load_devices()
        for record in devices.values():
            if hmac.compare_digest(record.token_sha256, digest):
                record = DeviceRecord(
                    device_id=record.device_id,
                    device_name=record.device_name,
                    token_sha256=record.token_sha256,
                    paired_at=record.paired_at,
                    last_seen=datetime.now(UTC).isoformat(),
                )
                devices[record.device_id] = record
                self._save_devices(devices)
                return record
        raise HTTPException(status_code=401, detail="unknown or forgotten token")

    async def open(self) -> None:
        if self._task is not None:
            raise RuntimeError("remote-control listener is already open")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            set_exclusive_bind(sock)
            sock.bind((self.bind_host, self.requested_port))
            sock.listen(16)
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        self._socket = sock
        self.port = int(sock.getsockname()[1])
        self.address, self.address_error = lan_address()
        config = uvicorn.Config(
            build_listener_app(self, engine_url="http://engine"),
            log_level="warning",
            access_log=False,
            lifespan="off",
            ws="none",
            limit_concurrency=LISTENER_CONCURRENCY,
            timeout_keep_alive=LISTENER_KEEP_ALIVE_S,
            timeout_graceful_shutdown=LISTENER_GRACEFUL_SHUTDOWN_S,
        )
        self._server = _QuietServer(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
        for _ in range(200):
            if self._server.started:
                return
            if self._task.done():
                break
            await asyncio.sleep(0.01)
        await self.close()
        raise RuntimeError("the remote-control listener did not start within 2 s")

    @property
    def is_open(self) -> bool:
        return self._task is not None and not self._task.done()

    async def close(self) -> None:
        server, task, sock = self._server, self._task, self._socket
        self._server = None
        self._task = None
        self._socket = None
        if server is not None:
            server.should_exit = True
        try:
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except TimeoutError:
                    task.cancel()
                    logger.warning("remote control: listener did not stop within 5 s; cancelled")
        finally:
            if sock is not None:
                sock.close()

    def close_soon(self) -> None:
        self._spawn(self.close())

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, sep, value = header.partition(" ")
    if not sep or scheme.lower() != "bearer" or value.strip() == "":
        return None
    return value.strip()


def build_listener_app(session: RemoteSession, engine_url: str) -> FastAPI:
    """The LAN remote-control app: pair and ping behind :class:`ListenerGuard`."""
    del engine_url  # reserved for a later listener-served page origin
    if session.port is None:
        raise RuntimeError("bind the listener before building its app")
    host = session.address or "127.0.0.1"
    allowed_origin = f"http://{host}:{session.port}"
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(ListenerGuard, allowed_origin=allowed_origin, max_body_bytes=MAX_BODY_BYTES)

    @app.exception_handler(StarletteHTTPException)
    async def _listener_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 405:
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.post(PAIR_PATH, status_code=201, response_model=PairOut)
    async def post_pair(body: PairBody) -> PairOut | JSONResponse:
        name = body.device_name.strip()
        if not DEVICE_NAME_LEN[0] <= len(name) <= DEVICE_NAME_LEN[1]:
            raise HTTPException(
                status_code=422,
                detail=f"device_name must be {DEVICE_NAME_LEN[0]} to {DEVICE_NAME_LEN[1]} characters",
            )
        if not session.check_code(body.code):
            left = 0 if session.status == "locked_out" else MAX_WRONG_CODES - session.wrong_codes
            return JSONResponse(status_code=403, content={"detail": "wrong_code", "tries_left": left})
        record, token = session.pair_device(name)
        return PairOut(device_id=record.device_id, device_name=record.device_name, token=token)

    @app.get(PING_PATH, response_model=PingOut)
    async def get_ping(request: Request) -> PingOut:
        record = session.authenticate(_bearer_token(request))
        return PingOut(device_id=record.device_id, device_name=record.device_name)

    return app


def devices_file_is_owner_only(path: Path) -> bool:
    """True when the on-disk devices file is owner-only (0600 on POSIX)."""
    return path.is_file() and is_owner_only(path)
