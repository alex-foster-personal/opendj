"""FastAPI router for recorded sets.

Plan 12-03 Step 3. Mountable router (:data:`router`) that exposes the
session + transition data model to Phase 11's SvelteKit app. Bind your
uvicorn app to ``127.0.0.1`` (CAT-05b).

Endpoints::

    GET    /api/sets                                -> list of SessionSummary
    GET    /api/sets/{session_id}                   -> Session JSON
    GET    /api/sets/{session_id}/timeline          -> NDJSON stream of events
    GET    /api/sets/{session_id}/transitions       -> transitions array
    POST   /api/sets/{session_id}/transitions/{idx}/label
        body {"class": "..."}                       -> appends to labels.jsonl
    GET    /api/sets/{session_id}/audio/{segment}   -> MP3 stream (localhost-only
                                                       when share_state='private')
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, AsyncIterator, Iterable, Literal

from fastapi import APIRouter, HTTPException, Path as FPath, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import paths as sets_paths
from .audio import (
    PathTraversalError,
    list_segments,
    resolve_segment_path,
)
from .classify import CLASS_LIST, read_transitions
from .label import append_label
from .recorder_service import RecorderConflict, RecorderService
from .sessions import Session, get_session, list_sessions, summary_to_dict


@asynccontextmanager
async def _router_lifespan(app: Any) -> AsyncIterator[None]:
    if getattr(app.state, "sets_recorder_service", None) is None:
        app.state.sets_recorder_service = RecorderService()
    yield
    service = getattr(app.state, "sets_recorder_service", None)
    if isinstance(service, RecorderService):
        service.stop_owned_on_shutdown()


router = APIRouter(
    prefix="/api/sets",
    tags=["sets"],
    lifespan=_router_lifespan,
)


_LOCALHOST_HOSTS: frozenset[str] = frozenset(
    {"127.0.0.1", "::1", "localhost", "testclient"}
)
"""Allowed client hosts for private-session audio.

``testclient`` is FastAPI's TestClient default and is treated as
localhost; in production uvicorn binds to 127.0.0.1 per CAT-05b so the
real remote would never match anyway.
"""


def _is_localhost(request: Request) -> bool:
    client_host = request.client.host if request.client else None
    return client_host in _LOCALHOST_HOSTS


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------


class LabelRequest(BaseModel):
    """Body for the relabel endpoint."""

    cls: str = Field(alias="class", min_length=1, max_length=32)
    labeler: str = Field(default="web_ui", max_length=64)


SourceName = Literal["djay_monitor", "rb_history"]
_SERVICE_INIT_LOCK = threading.Lock()


def _default_sources() -> list[SourceName]:
    return ["djay_monitor"]


class RecorderStartRequest(BaseModel):
    """Explicit real-capture configuration for the REC button."""

    session_id: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:_\d+)?$",
    )
    ffmpeg_device_idx: int = Field(ge=0)
    sources: list[SourceName] = Field(default_factory=_default_sources)


class RecorderStatus(BaseModel):
    active: bool
    session_id: str | None
    pid: int | None
    owned: bool
    recoverable: bool


class RecorderRecoveryRequest(BaseModel):
    expected_pid: int = Field(gt=0)


def _recorder_service(request: Request) -> RecorderService:
    service = getattr(request.app.state, "sets_recorder_service", None)
    if service is None:
        with _SERVICE_INIT_LOCK:
            service = getattr(request.app.state, "sets_recorder_service", None)
            if service is None:
                service = RecorderService()
                request.app.state.sets_recorder_service = service
    if not isinstance(service, RecorderService):
        raise RuntimeError("app.state.sets_recorder_service must be RecorderService")
    return service


def _validated_recorder_session_id(service: RecorderService, session_id: str) -> None:
    try:
        sets_paths.session_dir(session_id, root=service.sets_root)
    except sets_paths.SessionPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _session_or_not_found(session_id: str) -> Session:
    try:
        session = get_session(session_id)
    except sets_paths.SessionPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@router.get("/recorder", response_model=RecorderStatus)
async def api_recorder_status(request: Request) -> dict[str, Any]:
    return _recorder_service(request).status()


@router.post(
    "/recorder/start",
    response_model=RecorderStatus,
    status_code=201,
)
async def api_recorder_start(
    request: Request,
    body: RecorderStartRequest,
) -> dict[str, Any]:
    try:
        return _recorder_service(request).start(
            session_id=body.session_id,
            ffmpeg_device_idx=body.ffmpeg_device_idx,
            sources=tuple(body.sources),
        )
    except RecorderConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/recorder/{session_id}/stop", response_model=RecorderStatus)
async def api_recorder_stop(request: Request, session_id: str) -> dict[str, Any]:
    service = _recorder_service(request)
    _validated_recorder_session_id(service, session_id)
    try:
        return service.stop(session_id)
    except RecorderConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/recorder/{session_id}/recover", response_model=RecorderStatus)
async def api_recorder_recover(
    request: Request,
    session_id: str,
    body: RecorderRecoveryRequest,
) -> dict[str, Any]:
    service = _recorder_service(request)
    _validated_recorder_session_id(service, session_id)
    try:
        return service.recover_stale(
            session_id,
            body.expected_pid,
        )
    except RecorderConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("")
async def api_list_sessions() -> JSONResponse:
    summaries = list_sessions()
    return JSONResponse([summary_to_dict(s) for s in summaries])


@router.get("/{session_id}")
async def api_get_session(session_id: str) -> JSONResponse:
    session = _session_or_not_found(session_id)
    payload: dict[str, Any] = {
        "summary": summary_to_dict(session.summary),
        "manifest": session.manifest,
        "segments": [asdict(s) for s in list_segments(session_id)],
    }
    return JSONResponse(payload)


@router.get("/{session_id}/timeline")
async def api_timeline_stream(session_id: str) -> StreamingResponse:
    _session_or_not_found(session_id)

    session_dir = sets_paths.session_dir(session_id)
    jsonl = session_dir / "timeline.jsonl"
    if not jsonl.exists():
        # Empty timeline is valid; stream zero bytes.
        async def _empty() -> Iterable[bytes]:
            yield b""
        return StreamingResponse(_empty(), media_type="application/x-ndjson")

    def _stream() -> Iterable[bytes]:
        with jsonl.open("rb") as fh:
            for line in fh:
                yield line

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


@router.get("/{session_id}/transitions")
async def api_transitions(session_id: str) -> JSONResponse:
    _session_or_not_found(session_id)
    rows = read_transitions(session_id)
    return JSONResponse(rows)


@router.post("/{session_id}/transitions/{idx}/label")
async def api_relabel(
    session_id: str,
    idx: int,
    body: LabelRequest,
) -> JSONResponse:
    _session_or_not_found(session_id)
    if body.cls not in CLASS_LIST:
        raise HTTPException(
            status_code=400,
            detail=f"class {body.cls!r} not in {list(CLASS_LIST)}",
        )
    try:
        append_label(session_id, idx, body.cls, labeler=body.labeler)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "idx": idx, "class": body.cls})


@router.get("/{session_id}/audio/{segment}")
async def api_audio(
    request: Request,
    session_id: str,
    segment: str = FPath(..., description="audio_<iso>.mp3"),
) -> FileResponse:
    session = _session_or_not_found(session_id)
    share_state = session.summary.share_state
    if share_state == "private" and not _is_localhost(request):
        raise HTTPException(
            status_code=403,
            detail="session is private; audio only available to localhost clients",
        )
    try:
        path = resolve_segment_path(session_id, segment)
    except PathTraversalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.exists():
        raise HTTPException(status_code=404, detail="segment not found")
    return FileResponse(str(path), media_type="audio/mpeg", filename=segment)


__all__ = [
    "LabelRequest",
    "RecorderStartRequest",
    "RecorderRecoveryRequest",
    "RecorderStatus",
    "router",
]
