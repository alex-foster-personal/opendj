"""Outside surface of ``apps.lighting`` for delivery layers and the LIGHT-06 HTTP router.

Delivery layers such as ``apps.opendj_cli`` and ``apps.webui`` may import this module and the
contract, never ``apps.lighting.patch`` or ``apps.lighting.stage_feed`` directly. Network
monitor, stage live-mode and offline render names are re-exported here (same pattern as
``apps.autodj.http``).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from apps.lighting.contract import CONTRACT_VERSION, LIGHTING_FLAG_ID, ClockController, ClockSourceError
from apps.lighting.fixtures import FixtureProfile, ProfileError, load_profile
from apps.lighting.monitor import ARTNET_PORT, SACN_PORT, MonitorConfig, start_monitor
from apps.lighting.offline_render import dump_frames, load_analysis, render_show
from apps.lighting.output_api import (
    ArtNetAddress,
    ArtNetAddressError,
    ArtNetAddressErrorResponse,
    ArtNetAddressRequest,
    LightingDisabledResponse,
    get_artnet_address,
    set_artnet_address,
)
from apps.lighting.patch import (
    Patch,
    PatchError,
    VenueStore,
    default_patch_path,
    load_store,
    save_store,
    store_from_json,
    store_to_json,
)
from apps.lighting.runtime import (
    FEED_ENDED_CODE,
    SLOW_CONSUMER_CODE,
    DeckFeed,
    FeedFrame,
    LightingRuntime,
    ReplayFeed,
)
from apps.lighting.settings import HANDOVER_KEY, PALETTE_KEY, SettingError, setting
from apps.lighting.show_map import list_scene_catalog, load_palette, load_show_map
from apps.lighting.stage_feed import FrameFeed, parse_frame, parse_frame_message, stage_patch_body
from apps.lighting.stage_replay import replay_query

LIGHTING_DISABLED_MESSAGE = (
    "lighting output is off in this build (feature flag lighting.output is off; see GET /api/v1/flags)"
)
RUNTIME_STATE_ATTR = "lighting_runtime"
PATCH_PATH_ATTR = "lighting_patch_path"
PROFILES_DIR_ATTR = "lighting_profiles_dir"
FRAME_FEED_ATTR = "lighting_frame_feed"
FRAMES_CLOCK_ATTR = "lighting_frames_clock"
FRAMES_MAX_HZ = 30
BUILTIN_PROFILES_DIR = Path(__file__).parent / "fixtures" / "generic"


def lighting_refusal(app: Any) -> dict[str, str] | None:
    store = getattr(app.state, "feature_flags", None)
    if store is None or not store.enabled(LIGHTING_FLAG_ID):
        return {
            "error": "flag_disabled",
            "flag": LIGHTING_FLAG_ID,
            "message": LIGHTING_DISABLED_MESSAGE,
        }
    return None


def bind_lighting_runtime(app: Any, runtime: LightingRuntime | None = None) -> LightingRuntime:
    existing = getattr(app.state, RUNTIME_STATE_ATTR, None)
    if runtime is not None:
        bound = runtime
    elif isinstance(existing, LightingRuntime):
        bound = existing
    else:
        bound = LightingRuntime()
    controller = getattr(app.state, "lighting_clock", None)
    if controller is not None:
        bound.clock_provider = controller.snapshot
    setattr(app.state, RUNTIME_STATE_ATTR, bound)
    return bound


class LightingDeckStatus(BaseModel):
    deck: str
    bpm: float | None = None
    playing: bool = False
    section: str | None = None


class LightingStatus(BaseModel):
    flag: str = Field(default=LIGHTING_FLAG_ID)
    enabled: bool = True
    contract: str = Field(default=CONTRACT_VERSION)
    feed_connected: bool = False
    decks: list[LightingDeckStatus] = Field(default_factory=list)
    weights: dict[str, float] = Field(default_factory=dict)
    subscribers: int = 0
    palette: str = "cool"


class LightingSceneRef(BaseModel):
    id: str
    name: str


class LightingSettingRequest(BaseModel):
    value: str


class LightingSettingValue(BaseModel):
    key: str
    value: str
    previous: str
    allowed: list[str]


class LightingLook(BaseModel):
    scene_id: str | None = None
    phrase_label: str | None = None


@asynccontextmanager
async def _router_lifespan(app: Any) -> AsyncIterator[None]:
    runtime = bind_lighting_runtime(app)
    if lighting_refusal(app) is None:
        await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


router = APIRouter(prefix="/lighting", tags=["lighting"], lifespan=_router_lifespan)


@router.get(
    "/output/artnet/address",
    response_model=ArtNetAddress | None,
    responses={409: {"model": LightingDisabledResponse, "description": "lighting.output is off"}},
)
def api_get_artnet_address(request: Request) -> ArtNetAddress | None:
    """Read this daemon's Art-Net address, or null before any selection; no output is sent."""
    refusal = lighting_refusal(request.app)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    return get_artnet_address(request.app)


@router.put(
    "/output/artnet/address",
    response_model=ArtNetAddress,
    responses={
        400: {"model": ArtNetAddressErrorResponse, "description": "Art-Net address field out of range"},
        409: {"model": LightingDisabledResponse, "description": "lighting.output is off"},
    },
)
def api_put_artnet_address(request: Request, fields: ArtNetAddressRequest) -> ArtNetAddress:
    """Select net/subnet/universe for this daemon lifetime; no persistence or UDP delivery.

    Returns the fields and derived port_address. Invalid net returns 400
    lighting_artnet_bad_net; invalid subnet/universe returns 400 lighting_artnet_bad_subuni.
    Rejected requests leave the previous selection intact; a disabled flag returns 409.
    """
    refusal = lighting_refusal(request.app)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    try:
        return set_artnet_address(request.app, fields)
    except ArtNetAddressError as exc:
        raise HTTPException(status_code=400, detail=exc.detail.model_dump()) from exc


@router.get(
    "/status",
    response_model=LightingStatus,
    responses={409: {"description": "lighting.output is off"}},
)
def api_lighting_status(request: Request) -> dict[str, Any]:
    refusal = lighting_refusal(request.app)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    return bind_lighting_runtime(request.app).status()


@router.get(
    "/scenes",
    response_model=list[LightingSceneRef],
    responses={409: {"description": "lighting.output is off"}},
)
def api_lighting_scenes(request: Request) -> list[dict[str, str]]:
    refusal = lighting_refusal(request.app)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    return list_scene_catalog()


@router.get(
    "/look",
    response_model=LightingLook,
    responses={409: {"description": "lighting.output is off"}},
)
def api_lighting_look(request: Request) -> dict[str, str | None]:
    refusal = lighting_refusal(request.app)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    return bind_lighting_runtime(request.app).current_look()


@router.put(
    "/settings/{key}",
    response_model=LightingSettingValue,
    responses={
        400: {"description": "unknown setting or value outside the allowed set"},
        409: {"description": "lighting.output is off"},
    },
)
def api_put_lighting_setting(request: Request, key: str, body: LightingSettingRequest) -> dict[str, Any]:
    """Set a lighting variant (e.g. palette); frames switch on the next downbeat and the pick is logged."""
    refusal = lighting_refusal(request.app)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    runtime = bind_lighting_runtime(request.app)
    try:
        row = setting(key)
        previous = runtime.settings.get(row.key)
        if row.key == PALETTE_KEY:
            runtime.set_palette(body.value)
        elif row.key == HANDOVER_KEY:
            runtime.set_handover(body.value)
        else:
            runtime.settings.set(row.key, body.value)
    except SettingError as exc:
        detail = {
            "error": "lighting_setting_invalid",
            "key": exc.key,
            "value": exc.value,
            "allowed": list(exc.allowed),
            "message": str(exc),
        }
        raise HTTPException(status_code=400, detail=detail) from exc
    return {"key": row.key, "value": body.value, "previous": previous, "allowed": list(row.allowed)}


def _patch_path(app: Any) -> Path:
    return getattr(app.state, PATCH_PATH_ATTR, None) or default_patch_path()


def _user_profiles_dir(app: Any) -> Path:
    return getattr(app.state, PROFILES_DIR_ATTR, None) or default_patch_path().parent / "profiles"


def load_profiles(user_dir: Path | None = None) -> dict[str, tuple[FixtureProfile, str]]:
    """Built-in generic profiles plus user profiles in ``user_dir``, keyed by id with their source."""
    found: dict[str, tuple[FixtureProfile, str]] = {}
    for source, folder in (("builtin", BUILTIN_PROFILES_DIR), ("user", user_dir)):
        if folder is None or not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                profile = load_profile(path)
            except (OSError, ValueError, ProfileError):
                continue
            found.setdefault(profile.id, (profile, source))
    return found


def _read_store(app: Any) -> VenueStore:
    path = _patch_path(app)
    if not path.exists():
        return VenueStore("default", (Patch("default", ()),))
    return load_store(path)


class LightingProfileRef(BaseModel):
    id: str
    name: str
    kind: str
    source: str
    modes: dict[str, int]


@router.get("/patch", responses={422: {"description": "stored patch file is invalid"}})
def api_get_lighting_patch(request: Request) -> dict[str, Any]:
    """The venue store: every named patch and the active one."""
    try:
        return store_to_json(_read_store(request.app))
    except PatchError as exc:
        raise HTTPException(status_code=422, detail=_patch_error_detail(exc)) from exc


def _patch_error_detail(exc: PatchError) -> dict[str, Any]:
    return {"error": "lighting_patch_invalid", "errors": list(exc.errors), "message": "; ".join(exc.errors)}


@router.put("/patch", responses={422: {"description": "patch failed validation; errors name the fixture ids"}})
def api_put_lighting_patch(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Replace the whole venue store after validation; an invalid body leaves the stored patch unchanged."""
    profiles = {pid: row[0] for pid, row in load_profiles(_user_profiles_dir(request.app)).items()}
    try:
        store = store_from_json(body, "request body")
        save_store(_patch_path(request.app), store, profiles)
    except PatchError as exc:
        raise HTTPException(status_code=422, detail=_patch_error_detail(exc)) from exc
    return store_to_json(store)


@router.get("/profiles", response_model=list[LightingProfileRef])
def api_lighting_profiles(request: Request) -> list[dict[str, Any]]:
    """Built-in and user fixture profiles with each mode's channel count."""
    return [
        {
            "id": p.id,
            "name": p.name,
            "kind": p.kind,
            "source": source,
            "modes": {m.name: len(m.channels) for m in p.modes},
        }
        for p, source in load_profiles(_user_profiles_dir(request.app)).values()
    ]


async def _events_pump(websocket: WebSocket, sub: Any) -> None:
    try:
        while not sub.overflow.is_set():
            item = await sub.queue.get()
            if item is None:
                await websocket.close(code=sub.close_code, reason=sub.close_reason)
                return
            await websocket.send_text(json.dumps(item, separators=(",", ":")))
        await websocket.close(code=SLOW_CONSUMER_CODE, reason=sub.close_reason or "slow consumer")
    except WebSocketDisconnect:
        return


@router.websocket("/events")
async def api_lighting_events(websocket: WebSocket) -> None:
    refusal = lighting_refusal(websocket.app)
    if refusal is not None:
        await websocket.accept()
        await websocket.send_json({"type": "refused", "status": 409, "detail": refusal})
        await websocket.close(code=1008, reason="lighting.output is off")
        return

    runtime = bind_lighting_runtime(websocket.app)
    await websocket.accept()
    sub = runtime.hub.attach()
    pump: asyncio.Task[None] | None = None
    try:
        pump = asyncio.create_task(_events_pump(websocket, sub))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
    finally:
        if pump is not None:
            pump.cancel()
            with suppress(asyncio.CancelledError):
                await pump
        runtime.hub.detach(sub)


class FrameThrottle:
    """Admit at most ``max_hz`` frames per second against a nanosecond clock."""

    def __init__(self, max_hz: int = FRAMES_MAX_HZ) -> None:
        self.interval_ns = 1_000_000_000 // max_hz + 1
        self._next_ns: int | None = None

    def wait_ns(self, now_ns: int) -> int:
        """Nanoseconds until the next frame may be sent (0 means now)."""
        return 0 if self._next_ns is None else max(0, self._next_ns - now_ns)

    def mark(self, now_ns: int) -> None:
        """Record a send at ``now_ns``."""
        self._next_ns = now_ns + self.interval_ns

    def admit(self, now_ns: int) -> bool:
        """Mark and return True when a frame may be sent at ``now_ns``, else False."""
        if self.wait_ns(now_ns) > 0:
            return False
        self.mark(now_ns)
        return True


def frame_feed(app: Any) -> FrameFeed:
    """The app's shared newest-wins frame feed, created on first use."""
    feed = getattr(app.state, FRAME_FEED_ATTR, None)
    if not isinstance(feed, FrameFeed):
        feed = FrameFeed()
        setattr(app.state, FRAME_FEED_ATTR, feed)
    return feed


def patch_version(app: Any) -> str | None:
    """Digest of the active patch as served to the visualiser, or None when unreadable."""
    try:
        return str(stage_patch_body(_read_store(app))["version"])
    except (OSError, ValueError):
        return None


def _frame_message(app: Any, frame: dict[str, Any]) -> dict[str, Any]:
    return {**frame, "patch_version": patch_version(app)}


@router.get("/frame", responses={404: {"description": "no frame yet"}, 409: {"description": "lighting.output off"}})
def api_lighting_frame(request: Request) -> dict[str, Any]:
    """Newest composed fixture state with the current patch version."""
    refusal = lighting_refusal(request.app)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    latest = frame_feed(request.app).latest
    if latest is None:
        raise HTTPException(status_code=404, detail={"error": "no_frame", "message": "no lighting frame yet"})
    return _frame_message(request.app, latest)


async def _frames_pump(websocket: WebSocket, feed: FrameFeed, clock: Callable[[], int]) -> None:
    throttle = FrameThrottle()
    try:
        async for frame in feed.frames():
            wait = throttle.wait_ns(clock())
            if wait > 0:
                await asyncio.sleep(wait / 1e9)
            throttle.mark(clock())
            message = _frame_message(websocket.app, feed.latest or frame)
            await websocket.send_text(json.dumps(message, separators=(",", ":")))
    except WebSocketDisconnect:
        return


@router.websocket("/frames")
async def api_lighting_frames(websocket: WebSocket) -> None:
    """Stream composed fixture states, at most 30 per second, each with ``patch_version``."""
    refusal = lighting_refusal(websocket.app)
    if refusal is not None:
        await websocket.accept()
        await websocket.send_json({"type": "refused", "status": 409, "detail": refusal})
        await websocket.close(code=1008, reason=f"{LIGHTING_FLAG_ID} is off")
        return
    clock = getattr(websocket.app.state, FRAMES_CLOCK_ATTR, None) or time.monotonic_ns
    await websocket.accept()
    pump = asyncio.create_task(_frames_pump(websocket, frame_feed(websocket.app), clock))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
    finally:
        pump.cancel()
        with suppress(asyncio.CancelledError):
            await pump


class LightingClockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clock_source: StrictStr


class LightingClockOut(BaseModel):
    clock_source: str
    effective_deck: str | None
    unavailable_reason: str | None


class LightingClockErrorDetail(BaseModel):
    code: str
    message: str


class LightingFlagRefusal(BaseModel):
    error: str
    flag: str
    message: str


class LightingClockError(BaseModel):
    detail: LightingClockErrorDetail | LightingFlagRefusal


def create_clock_router(get_controller: Callable[[Request], ClockController]) -> APIRouter:
    router = APIRouter(prefix="/lighting/clock", tags=["lighting"])
    responses = {
        400: {"model": LightingClockError, "description": "lighting_clock_bad_deck"},
        409: {"model": LightingClockError, "description": "lighting.output is off or lighting_clock_deck_empty"},
    }

    @router.get("", response_model=LightingClockOut, responses=responses)
    def get_clock(request: Request) -> LightingClockOut:
        _require_clock_enabled(request)
        return LightingClockOut(**asdict(get_controller(request).read()))

    @router.put("", response_model=LightingClockOut, responses=responses)
    def put_clock(body: LightingClockIn, request: Request) -> LightingClockOut:
        _require_clock_enabled(request)
        try:
            state = get_controller(request).set_source(body.clock_source)
        except ClockSourceError as exc:
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
        return LightingClockOut(**asdict(state))

    return router


def _require_clock_enabled(request: Request) -> None:
    refusal = lighting_refusal(request.app)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)


__all__ = [
    "ARTNET_PORT",
    "FEED_ENDED_CODE",
    "FRAMES_CLOCK_ATTR",
    "FRAMES_MAX_HZ",
    "FRAME_FEED_ATTR",
    "LIGHTING_DISABLED_MESSAGE",
    "RUNTIME_STATE_ATTR",
    "SACN_PORT",
    "SLOW_CONSUMER_CODE",
    "DeckFeed",
    "FeedFrame",
    "FrameFeed",
    "FrameThrottle",
    "LightingRuntime",
    "MonitorConfig",
    "PatchError",
    "ReplayFeed",
    "bind_lighting_runtime",
    "create_clock_router",
    "default_patch_path",
    "dump_frames",
    "frame_feed",
    "lighting_refusal",
    "load_analysis",
    "load_palette",
    "load_profiles",
    "load_show_map",
    "load_store",
    "parse_frame",
    "parse_frame_message",
    "patch_version",
    "render_show",
    "replay_query",
    "router",
    "stage_patch_body",
    "start_monitor",
]
