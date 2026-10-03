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
    GET    /api/sets/{session_id}/soundcloud-export -> metadata-only tracklist
    POST   /api/sets/{session_id}/soundcloud-export
        body {"acknowledge_rights": true}           -> paste-ready comment after ack
"""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi import Path as FPath
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from . import paths as sets_paths
from .audio import (
    PathTraversalError,
    list_segments,
    resolve_segment_path,
)
from .capture import CaptureUnavailable, default_input_device
from .classify import CLASS_LIST, read_transitions
from .label import append_label
from .recorder_service import RecorderConflict, RecorderService, RememberedInputUnreadable
from .sessions import Session, get_session, list_sessions, summary_to_dict
from .share import (
    SetShareConfig,
    SetShareError,
    configured_share_base_url,
    publish_metadata_only,
    share_url,
)
from .soundcloud_export import (
    SessionNotFound,
    SoundcloudExport,
    build_soundcloud_export,
)
from .sources.opendj_source import SOURCE_NAME as OPENDJ_SOURCE_NAME
from .sources.opendj_source import DeckObservationError

_SHARE_TRACK_VALUE_FIELDS = ("title", "artist", "album")


def _share_timeline_event(event: object) -> dict[str, object] | None:
    """Return the public projection of one track-load event, never diagnostics."""
    if not isinstance(event, dict) or event.get("action") != "track_loaded":
        return None
    value = event.get("value")
    public_value = {
        field: value[field]
        for field in _SHARE_TRACK_VALUE_FIELDS
        if isinstance(value, dict) and isinstance(value.get(field), str)
    }
    return {
        field: event.get(field)
        for field in (
            "session_id",
            "timestamp_s",
            "wall_clock",
            "action",
            "source",
            "deck",
            "track_stable_id",
        )
    } | {"value": public_value}


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


_LOCALHOST_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
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


SourceName = Literal["djay_monitor", "rb_history", "opendj_decks"]
_SERVICE_INIT_LOCK = threading.Lock()


def _default_sources() -> list[SourceName]:
    return ["djay_monitor"]


class RecorderStartRequest(BaseModel):
    """Explicit real-capture configuration for the REC button.

    Exactly one audio input: ``device_name`` (what the REC picker sends,
    resolved to an index at start), or a raw ``ffmpeg_device_idx``; or
    ``capture_audio: false`` for a tracklist-only recording (SET-10).
    """

    session_id: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:_\d+)?$",
    )
    ffmpeg_device_idx: int | None = Field(default=None, ge=0)
    device_name: str | None = Field(default=None, min_length=1, max_length=256)
    capture_audio: bool = True
    sources: list[SourceName] = Field(default_factory=_default_sources)

    @model_validator(mode="after")
    def _one_audio_input(self) -> RecorderStartRequest:
        named = (self.ffmpeg_device_idx is not None) + (self.device_name is not None)
        if self.capture_audio and named != 1:
            raise ValueError("name exactly one of ffmpeg_device_idx or device_name")
        if not self.capture_audio and named != 0:
            raise ValueError("capture_audio false records no audio, so names no input")
        return self


class RecorderInputDevice(BaseModel):
    index: int
    name: str
    loopback: bool


class RecorderDevicesResponse(BaseModel):
    """The audio inputs REC can record from, and the one it preselects."""

    devices: list[RecorderInputDevice]
    default_name: str | None


class RecorderRememberedInput(BaseModel):
    """The input REC last started on: a named input, or none (tracklist only)."""

    kind: Literal["device", "none"]
    name: str | None = None


class RecorderRememberedInputResponse(BaseModel):
    """Wraps the choice so "nothing remembered yet" is a body, not a null one."""

    remembered: RecorderRememberedInput | None


class RecorderStatus(BaseModel):
    active: bool
    session_id: str | None
    pid: int | None
    owned: bool
    recoverable: bool


class RecorderRecoveryRequest(BaseModel):
    expected_pid: int = Field(gt=0)


class MetadataShareRequest(BaseModel):
    """Explicit acknowledgement that the resulting web view excludes audio."""

    confirm_metadata_only: Literal[True]


class MetadataShareResponse(BaseModel):
    """A browser URL for the non-audio set history presentation."""

    session_id: str
    share_state: Literal["shared_cloud"]
    share_url: str
    content: Literal["metadata_only"]


class SoundcloudTracklistRowModel(BaseModel):
    timestamp_s: float
    timestamp_label: str
    title: str | None
    artist: str | None
    track_stable_id: str | None
    source: str | None
    deck: str | None
    display_name: str


class SoundcloudExportResponse(BaseModel):
    kind: Literal["metadata_only"]
    session_id: str
    audio_upload: Literal["not_offered"]
    takeover: Literal["not_offered"]
    rights_position: Literal["unsettled"]
    licensing_reminder: str
    tracklist: list[SoundcloudTracklistRowModel]
    comment: str | None = None
    acknowledged: bool | None = None


class SoundcloudExportAckRequest(BaseModel):
    acknowledge_rights: bool = False


class DeckObservationsRequest(BaseModel):
    """A batch of Open DJ deck-state snapshots, oldest first.

    Snapshots stay untyped here on purpose. Pydantic would happily
    coerce the string "false" into ``False`` and 0 into ``0.0``, which
    is exactly the quiet reinterpretation this pipeline must not do, so
    validation belongs to :func:`~apps.sets.sources.opendj_wire.parse_snapshot`
    alone rather than being split across two disagreeing contracts.

    The cap is ten minutes of a 1 s cadence: enough for a tab that was
    backgrounded to flush its buffer, small enough to bound one request.
    """

    snapshots: list[dict[str, Any]] = Field(min_length=1, max_length=600)

    #: The recording these snapshots were sampled under. REQUIRED.
    #:
    #: The only thing that can bind a batch to a set. The client cannot do it:
    #: it can re-read the recorder before posting, and the recording can still
    #: stop and be replaced between that read and this request landing, or the
    #: post can come from a `pagehide` flush that never re-read anything. Both
    #: file one set's playback under another, and both are invisible from
    #: either side. Sent, it is checked below, where the answer cannot change
    #: underneath the caller.
    #:
    #: It was optional for one round, so an already-open browser running the
    #: shipped emitter would keep posting. That is the fallback that masks a
    #: failure: an unbound batch is not "compatible", it is a batch nobody can
    #: say belongs to this set, and accepting it files a dead session's
    #: playback under whichever set is recording now - silently, and exactly
    #: in the window where a stale tab is most likely. Codex found it on #709.
    #: A stale client now gets 422 and its user reloads, which is a loud,
    #: recoverable failure instead of a quiet, permanent one.
    session_id: str = Field(min_length=1, max_length=64)


def _recorder_service(request: Request) -> RecorderService:
    service = getattr(request.app.state, "sets_recorder_service", None)
    if service is None:
        with _SERVICE_INIT_LOCK:
            service = getattr(request.app.state, "sets_recorder_service", None)
            if service is None:
                service = RecorderService()
                request.app.state.sets_recorder_service = service
    if not isinstance(service, RecorderService):
        raise TypeError("app.state.sets_recorder_service must be RecorderService")
    return service


def _opendj_source(request: Request) -> Any:
    """The live Open DJ observer, or a 409 explaining why there isn't one."""
    try:
        return _recorder_service(request).active_source(OPENDJ_SOURCE_NAME)
    except RecorderConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _validated_recorder_session_id(service: RecorderService, session_id: str) -> None:
    try:
        sets_paths.session_dir(session_id, root=service.sets_root)
    except sets_paths.SessionPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _sets_root(request: Request) -> Path | None:
    root = getattr(request.app.state, "sets_root", None)
    if root is None or isinstance(root, Path):
        return root
    raise TypeError("app.state.sets_root must be pathlib.Path or None")


def _soundcloud_export_or_error(request: Request, session_id: str) -> SoundcloudExport:
    try:
        return build_soundcloud_export(session_id, sets_root=_sets_root(request))
    except sets_paths.SessionPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc


def _session_or_not_found(request: Request, session_id: str) -> Session:
    try:
        session = get_session(session_id, sets_root=_sets_root(request))
    except sets_paths.SessionPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _share_visible_session(request: Request, session_id: str) -> Session:
    """Keep private set metadata invisible to a configured share host."""
    session = _session_or_not_found(request, session_id)
    audience = getattr(request.state, "share_audience", "local")
    if audience == "share" and session.summary.share_state != "shared_cloud":
        raise HTTPException(status_code=404, detail="session not found")
    return session


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@router.get("/recorder", response_model=RecorderStatus)
def api_recorder_status(request: Request) -> dict[str, Any]:
    # Sync: status() can wait on the recorder lock while a start spawns
    # ffmpeg, and that wait must not stall the event loop.
    return _recorder_service(request).status()


@router.get("/recorder/devices", response_model=RecorderDevicesResponse)
def api_recorder_devices(request: Request) -> dict[str, Any]:
    """List audio inputs by name for the REC picker; 503 when unmeasurable.

    Sync on purpose: listing spawns ffmpeg, so it runs in the threadpool
    instead of stalling the event loop.
    """
    try:
        devices = _recorder_service(request).list_devices()
    except CaptureUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    default = default_input_device(devices)
    return {
        "devices": [asdict(device) for device in devices],
        "default_name": default.name if default is not None else None,
    }


@router.get(
    "/recorder/remembered-input",
    response_model=RecorderRememberedInputResponse,
)
def api_recorder_remembered_input(request: Request) -> dict[str, Any]:
    """The input REC last started on, kept by the daemon (null when unknown).

    Server-side because the desktop shell serves the UI from a per-launch
    loopback port, and browser storage forgets across ports (SET-10).
    """
    try:
        return {"remembered": _recorder_service(request).remembered_input()}
    except RememberedInputUnreadable as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/recorder/start",
    response_model=RecorderStatus,
    status_code=201,
)
def api_recorder_start(
    request: Request,
    body: RecorderStartRequest,
) -> dict[str, Any]:
    # Sync: a start resolves the input with ffmpeg and waits out the
    # capture startup check, both blocking.
    try:
        return _recorder_service(request).start(
            session_id=body.session_id,
            ffmpeg_device_idx=body.ffmpeg_device_idx,
            device_name=body.device_name,
            capture_audio=body.capture_audio,
            sources=tuple(body.sources),
        )
    except RecorderConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CaptureUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


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


# ---------------------------------------------------------------------------
# Open DJ own-deck observations
# ---------------------------------------------------------------------------


@router.post("/deck-observations", status_code=202)
async def api_submit_deck_observations(
    request: Request,
    body: DeckObservationsRequest,
) -> dict[str, Any]:
    """Ingest Open DJ deck-state snapshots into the live recording.

    The browser posts here on a timer; any agent can post the same
    payload, which is what keeps this flow drivable without a UI.
    Bad snapshots are rejected 422 rather than dropped, because a
    silently discarded observation is an under-counted set.
    """
    source = _opendj_source(request)
    if body.session_id != source.session_id:
        # A backlog that outlived its recording. Refusing is the only place
        # this can be caught: by the time the request arrives the client's own
        # check is already stale. 409 rather than 422 because the payload is
        # perfectly well formed -- it just belongs to a set that is over.
        raise HTTPException(
            status_code=409,
            detail=(
                f"snapshots were sampled under session {body.session_id!r} but "
                f"{source.session_id!r} is recording now; discarded rather than "
                "recorded against the wrong set"
            ),
        )
    try:
        accepted = source.submit_many(body.snapshots)
    except DeckObservationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"accepted": accepted, "status": source.status()}


@router.get("/deck-observations")
async def api_deck_observation_status(request: Request) -> dict[str, Any]:
    """Why a row did or did not appear: per-deck dwell against the threshold."""
    return _opendj_source(request).status()


@router.get("")
async def api_list_sessions(request: Request) -> JSONResponse:
    summaries = list_sessions(sets_root=_sets_root(request))
    if getattr(request.state, "share_audience", "local") == "share":
        summaries = [summary for summary in summaries if summary.share_state == "shared_cloud"]
    return JSONResponse([summary_to_dict(s) for s in summaries])


@router.get("/{session_id}")
async def api_get_session(request: Request, session_id: str) -> JSONResponse:
    session = _share_visible_session(request, session_id)
    if getattr(request.state, "share_audience", "local") == "share":
        return JSONResponse({"summary": summary_to_dict(session.summary)})
    payload: dict[str, Any] = {
        "summary": summary_to_dict(session.summary),
        "manifest": session.manifest,
        "segments": [asdict(s) for s in list_segments(session_id, sets_root=_sets_root(request))],
    }
    return JSONResponse(payload)


@router.get("/{session_id}/timeline")
async def api_timeline_stream(request: Request, session_id: str) -> StreamingResponse:
    _share_visible_session(request, session_id)

    session_dir = sets_paths.session_dir(session_id, root=_sets_root(request))
    jsonl = session_dir / "timeline.jsonl"
    if not jsonl.exists():
        # Empty timeline is valid; stream zero bytes.
        async def _empty() -> Iterable[bytes]:
            yield b""

        return StreamingResponse(_empty(), media_type="application/x-ndjson")

    def _stream() -> Iterable[bytes]:
        with jsonl.open("rb") as fh:
            yield from fh

    if getattr(request.state, "share_audience", "local") != "share":
        return StreamingResponse(_stream(), media_type="application/x-ndjson")

    def _share_stream() -> Iterable[bytes]:
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = _share_timeline_event(json.loads(line))
            except json.JSONDecodeError:
                continue
            if event is not None:
                yield json.dumps(event, separators=(",", ":")).encode() + b"\n"

    return StreamingResponse(_share_stream(), media_type="application/x-ndjson")


@router.get(
    "/{session_id}/soundcloud-export",
    response_model=SoundcloudExportResponse,
)
async def api_get_soundcloud_export(request: Request, session_id: str) -> JSONResponse:
    export = _soundcloud_export_or_error(request, session_id)
    return JSONResponse(export.to_dict(include_comment=True))


@router.post(
    "/{session_id}/soundcloud-export",
    response_model=SoundcloudExportResponse,
)
async def api_post_soundcloud_export(
    request: Request,
    session_id: str,
    body: SoundcloudExportAckRequest,
) -> JSONResponse:
    export = _soundcloud_export_or_error(request, session_id)
    if not body.acknowledge_rights:
        return JSONResponse(export.to_dict(include_comment=False), status_code=400)
    payload = export.to_dict(include_comment=True)
    payload["acknowledged"] = True
    return JSONResponse(payload)


@router.get("/{session_id}/transitions")
async def api_transitions(request: Request, session_id: str) -> JSONResponse:
    _share_visible_session(request, session_id)
    rows = read_transitions(session_id, sets_root=_sets_root(request))
    return JSONResponse(rows)


def _metadata_share_base_url(request: Request) -> str:
    set_share_config = getattr(request.app.state, "set_share_config", None)
    share_config = getattr(request.app.state, "share_config", None)
    share_host = getattr(share_config, "host", None)
    share_auth = getattr(share_config, "auth", None)
    if not isinstance(share_host, str) or not isinstance(share_auth, str):
        raise SetShareError("share gate configuration is missing")
    return configured_share_base_url(
        set_share_config if isinstance(set_share_config, SetShareConfig) else None,
        share_host=share_host,
        share_auth=share_auth,
    )


def _metadata_share_response(request: Request, session: Session) -> MetadataShareResponse:
    if session.summary.share_state != "shared_cloud":
        raise HTTPException(
            status_code=409,
            detail="metadata sharing has not been enabled for this set",
        )
    try:
        base_url = _metadata_share_base_url(request)
    except SetShareError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return MetadataShareResponse(
        session_id=session.summary.session_id,
        share_state="shared_cloud",
        share_url=share_url(session.summary.session_id, base_url),
        content="metadata_only",
    )


@router.get("/{session_id}/share", response_model=MetadataShareResponse)
async def api_get_metadata_share(request: Request, session_id: str) -> MetadataShareResponse:
    """Get an existing metadata-only share link for a finalized published set."""
    return _metadata_share_response(request, _share_visible_session(request, session_id))


@router.post("/{session_id}/share", response_model=MetadataShareResponse)
async def api_publish_metadata_share(
    request: Request,
    session_id: str,
    body: MetadataShareRequest,
) -> MetadataShareResponse:
    """Publish timeline metadata after explicit acknowledgement, never MP3 audio."""
    del body
    if getattr(request.state, "share_audience", "local") == "share":
        raise HTTPException(status_code=403, detail="share audience cannot publish set metadata")
    session = _session_or_not_found(request, session_id)
    try:
        _metadata_share_base_url(request)
        publish_metadata_only(
            sets_paths.session_dir(session.summary.session_id, root=_sets_root(request))
        )
    except SetShareError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _metadata_share_response(request, _session_or_not_found(request, session_id))


@router.post("/{session_id}/transitions/{idx}/label")
async def api_relabel(
    request: Request,
    session_id: str,
    idx: int,
    body: LabelRequest,
) -> JSONResponse:
    _session_or_not_found(request, session_id)
    if body.cls not in CLASS_LIST:
        raise HTTPException(
            status_code=400,
            detail=f"class {body.cls!r} not in {list(CLASS_LIST)}",
        )
    try:
        append_label(
            session_id,
            idx,
            body.cls,
            labeler=body.labeler,
            sets_root=_sets_root(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "idx": idx, "class": body.cls})


@router.get("/{session_id}/audio/{segment}")
async def api_audio(
    request: Request,
    session_id: str,
    segment: str = FPath(..., description="audio_<iso>.wav (odj-audio capture) or audio_<iso>.mp3 (ffmpeg)"),
) -> FileResponse:
    if getattr(request.state, "share_audience", "local") == "share":
        raise HTTPException(status_code=404, detail="segment not found")
    _share_visible_session(request, session_id)
    if not _is_localhost(request):
        raise HTTPException(
            status_code=403,
            detail="recorded audio is personal-review-only and unavailable to remote clients",
        )
    try:
        path = resolve_segment_path(session_id, segment, sets_root=_sets_root(request))
    except PathTraversalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.exists():
        raise HTTPException(status_code=404, detail="segment not found")
    media_type = sets_paths.SEGMENT_MEDIA_TYPES[path.suffix]
    return FileResponse(str(path), media_type=media_type, filename=segment)


__all__ = [
    "LabelRequest",
    "MetadataShareRequest",
    "MetadataShareResponse",
    "RecorderRecoveryRequest",
    "RecorderStartRequest",
    "RecorderStatus",
    "SoundcloudExportAckRequest",
    "SoundcloudExportResponse",
    "router",
]
