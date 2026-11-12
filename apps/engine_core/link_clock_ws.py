"""Dedicated WebSocket for Link session clock reads (issue #11429, parent #6371).

The page's clock poll no longer shares the browser HTTP/1.1 pool with every
other API call. One socket per page at ``/api/v1/link/clock/ws``; it does not
attach to the ``/api/v1/events`` hub.

Frames are ``{id, op: 'clock' | 'ping'}``. Success answers echo ``id`` plus the
same body as ``GET /api/v1/link/clock`` or ``POST /api/v1/link/clock/ping``.
HTTP refusal cases answer ``{id, status, detail}`` on this socket. When
``link.tempo_sync`` is off the socket closes with RFC 6455 1008 and the reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from apps.ableton_link import CarabinerClient
from apps.ableton_link.carabiner import carabiner_port
from apps.ableton_link.link_clock import DEFAULT_QUANTUM, beat_at_time, link_now_us, phase_of
from apps.b2b_sync.link_relay import LinkRelay
from apps.b2b_sync.link_roles import LaneLead, RoleAuthority
from apps.engine_core.ws import SHELL_ACK_PROTOCOL_ERROR_CODE

LINK_CLOCK_WS_PATH: str = "/api/v1/link/clock/ws"
LINK_FLAG_ID: str = "link.tempo_sync"
LINK_DISABLED: str = (
    "Ableton Link tempo sync is off in this build (feature flag link.tempo_sync is off; see GET /api/v1/flags)"
)
LINK_CLOCK_POLICY_ERROR_CODE: int = SHELL_ACK_PROTOCOL_ERROR_CODE

_LINK_CLIENT_KEY: str = "link_tempo_client"
_ROLE_KEY: str = "link_b2b_role"
_RELAY_KEY: str = "b2b_link_relay"
_ROLE_INIT_LOCK = asyncio.Lock()


class LinkClockResponseError(Exception):
    """A clock read that would have been HTTP 403/503/422."""

    def __init__(self, status: int, detail: str | dict[str, Any]) -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail if isinstance(detail, str) else json.dumps(detail))


def _flag_refusal(app: Any) -> str | None:
    store = getattr(app.state, "feature_flags", None)
    if store is None:
        return LINK_DISABLED
    try:
        enabled = store.enabled(LINK_FLAG_ID)
    except KeyError:
        return LINK_DISABLED
    if not enabled:
        return LINK_DISABLED
    return None


def _link_client(app: Any) -> CarabinerClient | None:
    client = getattr(app.state, _LINK_CLIENT_KEY, None)
    return client if isinstance(client, CarabinerClient) else None


def _relay_peers(app: Any) -> int:
    relay = getattr(app.state, _RELAY_KEY, None)
    return relay.relay_peers() if isinstance(relay, LinkRelay) else 0


async def _role_authority(app: Any) -> RoleAuthority:
    async with _ROLE_INIT_LOCK:
        current = getattr(app.state, _ROLE_KEY, None)
        if current is None:
            current = RoleAuthority(uuid.uuid4().hex)
            setattr(app.state, _ROLE_KEY, current)
        if not isinstance(current, RoleAuthority):
            raise TypeError(f"app.state.{_ROLE_KEY} holds {type(current).__name__}, not a RoleAuthority")
        return current


def _role_fields(authority: RoleAuthority) -> dict[str, Any]:
    snap = authority.snapshot()
    lead: LaneLead
    lead_reason: str
    lead, lead_reason = authority.lane_lead(now_s=time.monotonic())
    return {
        "role": snap["role"],
        "took_seq": snap["took_seq"],
        "role_reason": snap["reason"],
        "engine_id": snap["engine_id"],
        "lane_lead": lead,
        "lane_lead_reason": lead_reason,
    }


def _port_label() -> str:
    try:
        port = carabiner_port()
    except ValueError as exc:
        raise LinkClockResponseError(status=503, detail=str(exc)) from exc
    return f"127.0.0.1:{port}"


def build_link_clock_body(
    app: Any,
    client: CarabinerClient | None,
    *,
    role_fields: dict[str, Any],
    strict: bool = True,
) -> dict[str, Any]:
    """The ``GET /api/v1/link/clock`` body for this session (LINK-12)."""
    now_us = link_now_us()
    carabiner = client.address if client is not None else _port_label()
    base: dict[str, Any] = {
        "enabled": client is not None,
        "connected": False,
        "peers": None,
        "bpm": None,
        "quantum": DEFAULT_QUANTUM,
        "link_now_us": now_us,
        "start_us": None,
        "beat": None,
        "phase": None,
        "playing": None,
        **role_fields,
        "carabiner": carabiner,
        "error": None,
        "relay_peers": _relay_peers(app),
    }
    if client is None:
        return base
    snap = client.snapshot()
    if not snap.connected and not strict:
        base["error"] = snap.error or f"Carabiner on {client.address} has not reported the session yet"
        return base
    if not snap.connected:
        raise LinkClockResponseError(
            status=503,
            detail=snap.error or f"Carabiner on {client.address} has not reported the session yet",
        )
    if snap.start_us is None or snap.bpm is None:
        raise LinkClockResponseError(
            status=503,
            detail=f"Carabiner on {client.address} sent a status without :start/:bpm",
        )
    beat = beat_at_time(snap.start_us, snap.bpm, now_us)
    base.update(
        connected=True,
        peers=snap.peers,
        bpm=snap.bpm,
        start_us=snap.start_us,
        beat=beat,
        phase=phase_of(beat, DEFAULT_QUANTUM),
        playing=snap.playing,
    )
    return base


def build_link_ping_body() -> dict[str, Any]:
    """The ``POST /api/v1/link/clock/ping`` body."""
    return {"link_now_us": link_now_us()}


async def link_clock_body_for_app(app: Any, *, strict: bool = True) -> dict[str, Any]:
    authority = await _role_authority(app)
    role_fields = _role_fields(authority)
    client = _link_client(app)
    return await asyncio.to_thread(
        build_link_clock_body,
        app,
        client,
        role_fields=role_fields,
        strict=strict,
    )


async def _handle_frame(app: Any, raw: str) -> dict[str, Any]:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LinkClockResponseError(status=422, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise LinkClockResponseError(status=422, detail="frame must be a JSON object")
    frame_id = message.get("id")
    if not isinstance(frame_id, (str, int)) or isinstance(frame_id, bool):
        raise LinkClockResponseError(status=422, detail="frame id must be a string or number")
    op = message.get("op")
    if op not in ("clock", "ping"):
        raise LinkClockResponseError(
            status=422,
            detail=f"op must be 'clock' or 'ping', not {op!r}",
        )
    if op == "ping":
        body = await asyncio.to_thread(build_link_ping_body)
        return {"id": frame_id, **body}
    body = await link_clock_body_for_app(app, strict=True)
    return {"id": frame_id, **body}


async def link_clock_endpoint(websocket: WebSocket) -> None:
    """``/api/v1/link/clock/ws``: clock and ping off the HTTP/1.1 pool."""
    refusal = _flag_refusal(websocket.app)
    if refusal is not None:
        await websocket.accept()
        await websocket.close(code=LINK_CLOCK_POLICY_ERROR_CODE, reason=refusal[:123])
        return

    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                answer = await _handle_frame(websocket.app, raw)
            except LinkClockResponseError as exc:
                frame_id: Any | None = None
                with contextlib.suppress(Exception):
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        frame_id = parsed.get("id")
                if frame_id is None:
                    await websocket.close(
                        code=LINK_CLOCK_POLICY_ERROR_CODE,
                        reason="frame id required",
                    )
                    return
                detail = exc.detail
                if not isinstance(detail, str):
                    detail = json.dumps(detail, separators=(",", ":"))
                await websocket.send_json({"id": frame_id, "status": exc.status, "detail": detail})
                continue
            await websocket.send_json(answer)
    except WebSocketDisconnect:
        return


__all__ = [
    "LINK_CLOCK_POLICY_ERROR_CODE",
    "LINK_CLOCK_WS_PATH",
    "LINK_DISABLED",
    "LINK_FLAG_ID",
    "LinkClockResponseError",
    "build_link_clock_body",
    "build_link_ping_body",
    "link_clock_body_for_app",
    "link_clock_endpoint",
]
