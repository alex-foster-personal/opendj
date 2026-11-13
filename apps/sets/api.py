"""FastAPI router for recorded sets.

Plan 12-03 Step 3. Mountable router (:data:`router`) that exposes the
session + transition data model to Phase 11's SvelteKit app. Bind your
uvicorn app to ``127.0.0.1`` (CAT-05b).

:mod:`apps.webui.server.app_wiring` mounts this router at
:data:`SETS_API_PREFIX` and :data:`SETS_V1_API_PREFIX` (one router, two
prefixes). Example paths (each also exists under ``/api/v1/sets``)::

    GET    /api/sets                                -> list of SessionSummary
    GET    /api/sets/{session_id}                   -> Session JSON
    GET    /api/sets/{session_id}/timeline          -> NDJSON stream of events
    GET    /api/sets/{session_id}/transitions       -> transitions array
    POST   /api/sets/{session_id}/transitions/{idx}/label
        body {"class": "..."}                       -> appends to labels.jsonl
    GET    /api/sets/{session_id}/audio/{segment}   -> MP3 stream (localhost-only
                                                       when share_state='private')
    POST   /api/sets/recorder/{id}/master-pcm      -> one int16 chunk of the page's
                                                       master mix (SET-12)
    POST   /api/sets/recorder/{id}/stream-encoder  -> start the master-mix encoder
        body {"format": "mp3", "bitrate_kbps": 192} (STREAM-11486)
    GET    /api/sets/recorder/stream-encoder       -> {"encoder": status or null}
    POST   /api/sets/recorder/{id}/stream-encoder/stop -> stop it
    PUT    /api/sets/recorder/stream/rtmp          -> configure RTMP push (STREAM-11743)
    GET    /api/sets/recorder/stream/rtmp          -> {"config": public dict or null}
    POST   /api/sets/recorder/{id}/stream          -> start sink rtmp
    GET    /api/sets/recorder/stream               -> {"stream": status or null}
    POST   /api/sets/recorder/{id}/stream/stop     -> stop the stream sink
    GET    /api/sets/{session_id}/soundcloud-export -> metadata-only tracklist
    POST   /api/sets/{session_id}/soundcloud-export
        body {"acknowledge_rights": true}           -> paste-ready comment after ack
    GET    /api/sets/{session_id}/share            -> metadata-only share link (SET-08)
    POST   /api/sets/{session_id}/share
        body {"confirm_metadata_only": true}       -> publish metadata-only share (SET-08)
    POST   /api/sets/{session_id}/split
        body {"out_dir": "/abs/path"}               -> per-track WAV files from the joined
                                                       recording (built from WAV segments when absent)
    GET    /api/sets/{session_id}/tracklist        -> tracklist JSON
    GET    /api/sets/{session_id}/tracklist.{fmt}  -> tracklist export (txt, md, csv, …)
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response
from fastapi import Path as FPath
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from apps.shared.state.tracklist_hidden import HIDDEN_LABEL, hidden_stable_ids_at
from apps.stream.config import (
    DEFAULT_AUDIO_KBPS,
    DEFAULT_VIDEO_SIZE,
    RtmpConfigRefused,
)
from apps.stream.encoder import (
    DEFAULT_BITRATE_KBPS,
    MAX_BITRATE_KBPS,
    MIN_BITRATE_KBPS,
    EncoderRequestRefused,
    EncoderUnavailable,
    StreamSourceNotMaster,
)
from apps.stream.service import StreamConflict, StreamSinkUnknown

from . import paths as sets_paths
from . import portable
from .api_controls import router as controls_router
from .api_deps import _session_or_not_found, _sets_root, _share_visible_session
from .audio import (
    PathTraversalError,
    list_segments,
    resolve_segment_path,
)
from .capture import CaptureUnavailable, default_input_device
from .classify import CLASS_LIST, read_transitions
from .label import append_label
from .master_mix import MasterMixChunkRefused, MasterMixRecordingStopped
from .master_tap import MasterTapUnavailable, master_tap_channel
from .record import TimelineCorruptionError, TimelineJsonl
from .recorder_service import (
    RecorderConflict,
    RecorderRequestInvalid,
    RecorderService,
    RecorderStartFailed,
    RecordSource,
    RememberedInputUnreadable,
)
from .sessions import Session, list_sessions, summary_to_dict
from .share import (
    SetShareConfig,
    SetShareError,
    configured_share_base_url,
    publish_metadata_only,
    share_url,
)
from .soundcloud_export import (
    LICENSING_REMINDER,
    SessionNotFound,
    SoundcloudExport,
    build_soundcloud_export,
)
from .sources.control_event_source import ControlEventIngestError, ControlEventSource
from .sources.opendj_source import SOURCE_NAME as OPENDJ_SOURCE_NAME
from .sources.opendj_source import DeckObservationError
from .split_tracks import SplitRefused, split_recording
from .state import SetsState
from .tracklist_export import (
    FILE_FORMATS,
    LibraryTrack,
    TracklistFormat,
    TrackLookup,
    build_tracklist,
    render_tracklist,
    tracklist_filename,
    tracklist_media_type,
)

_LOG = logging.getLogger(__name__)

_SHARE_TRACK_VALUE_FIELDS = ("title", "artist", "album")


def _hidden_stable_ids_for_request(request: Request) -> frozenset[str]:
    raw = getattr(request.app.state, "state_db_path", None)
    if raw is None:
        raise HTTPException(status_code=500, detail="state database not configured")
    try:
        return hidden_stable_ids_at(Path(raw))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="state database not found") from exc


def _share_timeline_event(
    event: object,
    hidden_stable_ids: frozenset[str],
) -> dict[str, object] | None:
    """Return the public projection of one track-load event, never diagnostics."""
    if not isinstance(event, dict) or event.get("action") != "track_loaded":
        return None
    value = event.get("value")
    public_value = {
        field: value[field]
        for field in _SHARE_TRACK_VALUE_FIELDS
        if isinstance(value, dict) and isinstance(value.get(field), str)
    }
    stable_id = event.get("track_stable_id")
    if isinstance(stable_id, str) and stable_id in hidden_stable_ids:
        public_value = {"title": HIDDEN_LABEL, "artist": HIDDEN_LABEL}
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


def _redact_timeline_line(line: str, hidden_stable_ids: frozenset[str]) -> str:
    """Re-encode one timeline.jsonl line when any loaded track is hidden."""
    if not hidden_stable_ids or not line.strip():
        return line
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return line
    stable_id = event.get("track_stable_id")
    value = event.get("value")
    if (
        isinstance(stable_id, str)
        and stable_id in hidden_stable_ids
        and isinstance(value, dict)
    ):
        redacted = dict(event)
        public_value = {**value, "title": HIDDEN_LABEL, "artist": HIDDEN_LABEL}
        public_value.pop("album", None)
        redacted["value"] = public_value
        return json.dumps(redacted, separators=(",", ":"))
    public = _share_timeline_event(event, hidden_stable_ids)
    if public is None:
        return line
    return json.dumps(public, separators=(",", ":"))


@asynccontextmanager
async def _router_lifespan(app: Any) -> AsyncIterator[None]:
    if getattr(app.state, "sets_recorder_service", None) is None:
        app.state.sets_recorder_service = RecorderService()
    yield
    service = getattr(app.state, "sets_recorder_service", None)
    if isinstance(service, RecorderService):
        service.stop_owned_on_shutdown()


SETS_API_PREFIX = "/api/sets"
SETS_V1_API_PREFIX = "/api/v1/sets"

router = APIRouter(
    tags=["sets"],
    lifespan=_router_lifespan,
)
# Read-only recorded-controls routes (SETREC, PR #14729) live in api_controls.
router.include_router(controls_router)


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
    """Explicit capture configuration for the REC button (SET-10, SET-12).

    ``source`` is required, never defaulted: ``master`` records the app's own
    master bus (streamed in by the page, see ``/master-pcm``); ``loopback``
    and ``external`` record exactly one input, ``device_name`` (what the REC
    picker sends, resolved to an index at start) or a raw
    ``ffmpeg_device_idx``; ``none`` records the tracklist only.
    """

    session_id: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:_\d+)?$",
    )
    source: RecordSource = Field(
        description=(
            "master: Open DJ's own master mix (post master fader, never the cue); "
            "loopback: a loopback input such as BlackHole; external: another input, "
            "e.g. an audio interface carrying a hardware mixer; none: tracklist only."
        )
    )
    ffmpeg_device_idx: int | None = Field(default=None, ge=0)
    device_name: str | None = Field(default=None, min_length=1, max_length=256)
    sources: list[SourceName] = Field(default_factory=_default_sources)

    @model_validator(mode="after")
    def _input_matches_source(self) -> RecorderStartRequest:
        named = (self.ffmpeg_device_idx is not None) + (self.device_name is not None)
        if self.source in ("loopback", "external") and named != 1:
            raise ValueError(
                f"source {self.source} names exactly one of ffmpeg_device_idx or device_name"
            )
        if self.source in ("master", "none") and named != 0:
            raise ValueError(f"source {self.source} records no input, so names none")
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
    """The source REC last started on: the master mix, a named input, or none
    (tracklist only)."""

    kind: Literal["master", "device", "none"]
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
    capture: Literal[
        "none", "unknown", "starting", "waiting_permission", "recording", "stopped", "failed"
    ] = Field(
        description=(
            "The audio capture of the recording: none (not recording, or tracklist only), "
            "unknown (owned by another process), waiting_permission (macOS's microphone "
            "prompt is up and nothing is written yet), recording, or failed (SET-11)."
        )
    )
    capture_error: str | None = Field(
        description=(
            "Always present: why the capture failed in the engine's words (for example "
            "microphone access turned off at the macOS prompt) when capture is failed "
            "and the engine said why; null for idle, external, and every non-failed "
            "capture state (SET-11)."
        ),
    )
    capture_source: RecordSource | None = Field(
        default=None,
        description=(
            "What the owned recording records (master, loopback, external, none); null "
            "when idle or owned by another process. A master recording is fed by the "
            "/performance page, which attaches its master tap when it reads this (SET-12)."
        ),
    )
    recordings_dir: str | None = Field(
        default=None,
        description="The directory every session folder is written under.",
    )
    peak_dbfs: float | None = Field(
        default=None,
        description=(
            "Session peak of the master mix in dBFS, 20 log10(|peak| / 32768) over accepted "
            "int16 chunks (issue #10371); null when this process records no master-mix audio."
        ),
    )
    peak_dbfs_recent: float | None = Field(
        default=None,
        description="Peak dBFS over the last 3 s of accepted master-mix audio; null as peak_dbfs.",
    )
    clipped_samples: int | None = Field(
        default=None,
        description="Master-mix samples at full scale (+32767 or -32768); null as peak_dbfs.",
    )
    clip_events: int | None = Field(
        default=None,
        description=(
            "Runs of 3 or more consecutive master-mix frames with a full-scale sample; "
            "null as peak_dbfs."
        ),
    )
    elapsed_s: float | None = Field(
        default=None,
        description="Seconds since the owned recording started; null when idle or not owned.",
    )
    audio_bytes: int | None = Field(
        default=None,
        description="Master-mix PCM bytes accepted so far; null when no master-mix audio is recorded here.",
    )


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


class TracklistRowModel(BaseModel):
    position: int
    start_s: float
    start_label: str
    artist: str | None
    title: str | None
    display_name: str
    key: str | None
    played_s: float | None
    played_label: str | None
    transition_label: str | None
    hidden: bool
    track_stable_id: str | None
    source: str | None
    deck: str | None
    duration_ms: int | None


class TracklistResponse(BaseModel):
    session_id: str
    rows: list[TracklistRowModel]


class ControlEventsRequest(BaseModel):
    """A batch of control-event v2 rows, oldest (lowest seq) first."""

    session_id: str = Field(min_length=1, max_length=64)
    events: list[dict[str, Any]] = Field(min_length=1, max_length=600)


class SplitRequest(BaseModel):
    out_dir: str | None = None


class TrackFileResponse(BaseModel):
    name: str
    path: str
    start_t_s: float
    duration_s: float
    size_bytes: int


class SplitResponse(BaseModel):
    session_id: str
    track_files: list[TrackFileResponse]


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


_CONTROL_EVENT_SOURCES: dict[str, ControlEventSource] = {}
_CONTROL_EVENT_SOURCES_LOCK = threading.Lock()


def _control_event_source(request: Request, session_id: str) -> ControlEventSource:
    service = _recorder_service(request)
    with service._lock:
        recorder = service._recorder
        if recorder is None:
            raise HTTPException(
                status_code=409,
                detail="no HTTP-owned recorder is active; control events need a live REC session",
            )
        if recorder.session_id != session_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"events were sampled under session {session_id!r} but "
                    f"{recorder.session_id!r} is recording now"
                ),
            )
        db_path = service.db_path
        session_dir = sets_paths.session_dir(session_id, root=service.sets_root)
    with _CONTROL_EVENT_SOURCES_LOCK:
        existing = _CONTROL_EVENT_SOURCES.get(session_id)
        if existing is not None:
            return existing
        source = ControlEventSource(
            session_id=session_id,
            state=SetsState(db_path=db_path),
            session_dir=session_dir,
        )
        _CONTROL_EVENT_SOURCES[session_id] = source
        return source


def _validated_recorder_session_id(service: RecorderService, session_id: str) -> None:
    try:
        sets_paths.session_dir(session_id, root=service.sets_root)
    except sets_paths.SessionPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _soundcloud_export_or_error(request: Request, session_id: str) -> SoundcloudExport:
    try:
        return build_soundcloud_export(
            session_id,
            sets_root=_sets_root(request),
            hidden_stable_ids=_hidden_stable_ids_for_request(request),
        )
    except sets_paths.SessionPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc


def _library_lookup(request: Request) -> TrackLookup | None:
    backend = getattr(request.app.state, "backend", None)
    bulk = getattr(backend, "get_tracks_bulk", None)
    if not callable(bulk):
        return None

    def lookup(ids: Sequence[str]) -> Mapping[str, LibraryTrack]:
        try:
            raw = bulk(list(ids))
        except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            _LOG.warning("tracklist library lookup failed: %s", exc)
            return {}
        out: dict[str, LibraryTrack] = {}
        for stable_id, track in raw.items():
            if not isinstance(stable_id, str):
                continue
            artist = getattr(track, "artist", None)
            title = getattr(track, "title", None)
            key = getattr(track, "key", None)
            duration_ms = getattr(track, "duration_ms", None)
            file_path = getattr(track, "file_path", None)
            out[stable_id] = LibraryTrack(
                artist=artist if isinstance(artist, str) else None,
                title=title if isinstance(title, str) else None,
                key=key if isinstance(key, str) else None,
                duration_ms=duration_ms if isinstance(duration_ms, int) else None,
                file_path=file_path if isinstance(file_path, str) else None,
            )
        return out

    return lookup


async def _tracklist_or_error(
    request: Request,
    session_id: str,
    *,
    with_library: bool,
) -> Any:
    _share_visible_session(request, session_id)
    lookup = _library_lookup(request) if with_library else None

    def _build() -> Any:
        return build_tracklist(
            session_id,
            sets_root=_sets_root(request),
            track_lookup=lookup,
        )

    try:
        return await run_in_threadpool(_build)
    except sets_paths.SessionPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc


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


#: The kill switch for the whole REC feature (SET-12 v1 fallback).
RECORDING_FLAG_ID = "sets.recording"
RECORDING_DISABLED = (
    "set recording is disabled in this build (feature flag sets.recording is off; "
    "see GET /api/v1/flags)"
)


def recording_refusal(request: Request) -> str | None:
    """Why REC cannot start in this build, or None.

    Both production apps mount ``app.state.feature_flags``; an app without a
    store (a bare test app) resolves every flag to its declared default, ON.
    """
    store = getattr(request.app.state, "feature_flags", None)
    if store is None or store.enabled(RECORDING_FLAG_ID):
        return None
    return RECORDING_DISABLED


async def _finalize_master_attach_failure(
    service: Any,
    session_id: str,
    reason: str,
) -> None:
    """Note attach failure and stop so the session dir holds an inspectable reason (SETREC-23)."""
    service.note_master_attach_failure(session_id, reason)
    await run_in_threadpool(service.stop, session_id)


@router.post(
    "/recorder/start",
    response_model=RecorderStatus,
    status_code=201,
)
async def api_recorder_start(
    request: Request,
    body: RecorderStartRequest,
) -> dict[str, Any]:
    """Start REC. A ``master`` start pushes the attach to the open page and
    answers only once its tap is connected, so the WAV begins within one
    order round trip of this request (SET-12); with no page to ask, nothing
    starts and the answer is 503 with the reason."""
    refusal = recording_refusal(request)
    if refusal is not None:
        raise HTTPException(status_code=403, detail=refusal)
    service = _recorder_service(request)
    channel = None
    if body.source == "master":
        try:
            channel = master_tap_channel(request)
        except MasterTapUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        reason = channel.unavailable_reason(request)
        if reason is not None:
            raise HTTPException(status_code=503, detail=reason)
    try:
        # In the threadpool: a device start resolves the input with ffmpeg and
        # waits out the capture startup check, both blocking.
        status = await run_in_threadpool(
            service.start,
            session_id=body.session_id,
            source=body.source,
            ffmpeg_device_idx=body.ffmpeg_device_idx,
            device_name=body.device_name,
            sources=tuple(body.sources),
        )
    except RecorderConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RecorderRequestInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CaptureUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RecorderStartFailed as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if channel is None:
        return status
    session_id = status["session_id"]
    try:
        await channel.attach(request, session_id)
        service.mark_master_tap_attached(session_id)
    except MasterTapUnavailable as exc:
        reason = str(exc)
        await _finalize_master_attach_failure(service, session_id, reason)
        raise HTTPException(status_code=503, detail=f"the master mix could not be tapped: {exc}") from exc
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            reason = "master tap attach was cancelled before it connected"
        else:
            reason = f"master tap attach failed: {type(exc).__name__}: {exc}"
        await _finalize_master_attach_failure(service, session_id, reason)
        raise
    return await run_in_threadpool(service.status)


@router.post(
    "/recorder/{session_id}/master-pcm",
    status_code=204,
    response_class=Response,
    responses={200: {"description": "dropped: the recording was cleanly stopped while this chunk was in flight"}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def api_recorder_master_pcm(
    request: Request,
    session_id: str,
    stream: str = Query(min_length=1, max_length=64),
    seq: int = Query(ge=0),
    sample_rate: int = Query(ge=8_000, le=192_000),
) -> Response:
    """One chunk of the page's master mix for a ``source: master`` recording (SET-12).

    The body is interleaved little-endian int16 stereo frames. 204 written;
    200 ``{"dropped": "recording_stopped"}`` when its recording was cleanly
    stopped while the chunk was in flight (the page ends quietly); 415 when
    ``Content-Type`` is not ``application/octet-stream``; 409 when no owned master
    recording has this id, or the chunk would leave a hole (a lost or
    out-of-order chunk); the page stops its tap and says so.
    """
    service = _recorder_service(request)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/octet-stream":
        raise HTTPException(
            status_code=415,
            detail="master PCM chunks must use application/octet-stream",
        )
    _validated_recorder_session_id(service, session_id)
    pcm = await request.body()
    try:
        await run_in_threadpool(
            service.write_master_pcm,
            session_id,
            stream=stream,
            seq=seq,
            sample_rate=sample_rate,
            pcm=pcm,
        )
    except MasterMixRecordingStopped:
        # The page's last chunk was in flight at a clean stop: expected, so it
        # is dropped and answered 200 with a reason the page ends on quietly.
        # Not a 4xx: a browser logs every 4xx as a console error (SET-12).
        return JSONResponse(status_code=200, content={"dropped": "recording_stopped", "session_id": session_id})
    except (RecorderConflict, MasterMixChunkRefused) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=204)


class StreamEncoderStartRequest(BaseModel):
    format: Literal["mp3", "aac", "opus"]
    bitrate_kbps: int = Field(DEFAULT_BITRATE_KBPS, ge=MIN_BITRATE_KBPS, le=MAX_BITRATE_KBPS)


@router.get("/recorder/stream-encoder")
def api_stream_encoder_status(request: Request) -> dict[str, Any]:
    """The master-mix stream encoder's status, or null when none was started."""
    return {"encoder": _recorder_service(request).stream_encoder_status()}


@router.post("/recorder/{session_id}/stream-encoder", status_code=201)
async def api_stream_encoder_start(
    request: Request, session_id: str, body: StreamEncoderStartRequest
) -> dict[str, Any]:
    """Tee a ``source: master`` recording into an encoder (STREAM-11486)."""
    service = _recorder_service(request)
    _validated_recorder_session_id(service, session_id)
    try:
        return await run_in_threadpool(
            service.start_stream_encoder, session_id, fmt=body.format, bitrate_kbps=body.bitrate_kbps
        )
    except StreamSourceNotMaster as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
    except EncoderUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)}) from exc
    except EncoderRequestRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RecorderConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/recorder/{session_id}/stream-encoder/stop")
async def api_stream_encoder_stop(request: Request, session_id: str) -> dict[str, Any]:
    service = _recorder_service(request)
    _validated_recorder_session_id(service, session_id)
    try:
        return await run_in_threadpool(service.stop_stream_encoder)
    except RecorderConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class StreamRtmpConfigRequest(BaseModel):
    url: str
    key: str
    image: str | None = None
    video_size: str = DEFAULT_VIDEO_SIZE
    audio_kbps: int = DEFAULT_AUDIO_KBPS


@router.put("/recorder/stream/rtmp")
async def api_stream_rtmp_configure(request: Request, body: StreamRtmpConfigRequest) -> dict[str, Any]:
    """Store RTMP URL, key and optional still image for the next push (STREAM-11743)."""
    service = _recorder_service(request)
    try:
        return await run_in_threadpool(
            service.stream.configure_rtmp,
            url=body.url,
            key=body.key,
            image=body.image,
            video_size=body.video_size,
            audio_kbps=body.audio_kbps,
        )
    except RtmpConfigRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StreamConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/recorder/stream/rtmp")
def api_stream_rtmp_config(request: Request) -> dict[str, Any]:
    return {"config": _recorder_service(request).stream.rtmp_config()}


class StreamStartRequest(BaseModel):
    sink: Literal["rtmp"]


@router.get("/recorder/stream")
def api_stream_status(request: Request) -> dict[str, Any]:
    return {"stream": _recorder_service(request).stream_status()}


@router.post("/recorder/{session_id}/stream", status_code=201)
async def api_stream_start(
    request: Request, session_id: str, body: StreamStartRequest
) -> dict[str, Any]:
    """Tee a ``source: master`` recording into an RTMP push (STREAM-11743)."""
    service = _recorder_service(request)
    _validated_recorder_session_id(service, session_id)
    try:
        return await run_in_threadpool(service.start_stream, session_id, sink=body.sink)
    except StreamSourceNotMaster as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
    except EncoderUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)}) from exc
    except (RtmpConfigRefused, StreamSinkUnknown) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (RecorderConflict, StreamConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/recorder/{session_id}/stream/stop")
async def api_stream_stop(request: Request, session_id: str) -> dict[str, Any]:
    service = _recorder_service(request)
    _validated_recorder_session_id(service, session_id)
    try:
        return await run_in_threadpool(service.stop_stream)
    except (RecorderConflict, StreamConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/recorder/{session_id}/stop", response_model=RecorderStatus)
async def api_recorder_stop(request: Request, session_id: str) -> dict[str, Any]:
    service = _recorder_service(request)
    _validated_recorder_session_id(service, session_id)
    try:
        status = service.stop(session_id)
    except RecorderConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if status["capture"] == "failed":
        raise HTTPException(status_code=409, detail=status)
    return status


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
# Control-event v2 ingest (SETREC-11827 FX, SETREC-08 stems; shared SETREC slices)
# ---------------------------------------------------------------------------


@router.post("/control-events", status_code=202)
async def api_submit_control_events(
    request: Request,
    body: ControlEventsRequest,
) -> dict[str, Any]:
    """Ingest page-owned control moves into controls.jsonl and sets.db."""
    source = _control_event_source(request, body.session_id)
    try:
        accepted = source.submit_many(body.events)
    except ControlEventIngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"accepted": accepted, "session_id": body.session_id}


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
        detail = (
            f"snapshots were sampled under session {body.session_id!r} but "
            f"{source.session_id!r} is recording now; discarded rather than "
            "recorded against the wrong set"
        )
        source.note_last_error(detail)
        raise HTTPException(status_code=409, detail=detail)
    try:
        accepted = source.submit_many(body.snapshots)
    except DeckObservationError as exc:
        message = str(exc)
        source.note_last_error(message)
        raise HTTPException(status_code=422, detail=message) from exc
    return {"accepted": accepted, "status": source.status()}


@router.get("/deck-observations")
async def api_deck_observation_status(request: Request) -> dict[str, Any]:
    """Why a row did or did not appear: per-deck dwell against the threshold."""
    return _opendj_source(request).status()


class SetImportResponse(BaseModel):
    session_id: str
    requested_session_id: str
    renamed: bool
    event_count: int
    message: str


def _library_track_lookup(request: Request) -> portable.TrackLookup | None:
    """Resolve artist/title/hidden from the library (#10337: tracklist_hidden)."""
    backend = getattr(request.app.state, "backend", None)
    if backend is None:
        return None

    def _lookup(ids: Sequence[str]) -> Mapping[str, portable.TrackInfo]:
        tracks = backend.get_tracks_bulk(list(ids))
        out: dict[str, portable.TrackInfo] = {}
        for stable_id, track in tracks.items():
            out[stable_id] = portable.TrackInfo(
                artist=getattr(track, "artist", None),
                title=getattr(track, "title", None),
                hidden=bool(getattr(track, "tracklist_hidden", False)),
            )
        return out

    return _lookup


def _portable_error_response(exc: portable.SetDocumentError) -> JSONResponse:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if isinstance(exc, portable.SchemaVersionUnsupported):
        detail["document_version"] = exc.document_version
        detail["supported_version"] = exc.supported_version
    elif isinstance(exc, portable.SetDocumentInvalid):
        detail["path"] = exc.path
    return JSONResponse(status_code=422, content={"detail": detail})


@router.post("/import", status_code=201, response_model=SetImportResponse)
async def api_import_set(
    request: Request,
    document: Annotated[dict[str, Any], Body()],
) -> JSONResponse:
    if getattr(request.state, "share_audience", "local") == "share":
        raise HTTPException(status_code=403, detail="share audience cannot import sets")

    def _work() -> dict[str, Any]:
        try:
            result = portable.import_set_document(document, sets_root=_sets_root(request))
        except sets_paths.SessionPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except portable.SetDocumentError:
            raise
        return {
            "session_id": result.session_id,
            "requested_session_id": result.requested_session_id,
            "renamed": result.renamed,
            "event_count": result.event_count,
            "message": result.message,
        }

    try:
        payload = await run_in_threadpool(_work)
    except portable.SetDocumentError as exc:
        return _portable_error_response(exc)
    except HTTPException:
        raise
    return JSONResponse(payload, status_code=201)


@router.get("/{session_id}/export")
async def api_export_set(request: Request, session_id: str) -> JSONResponse:
    if getattr(request.state, "share_audience", "local") == "share":
        raise HTTPException(status_code=404, detail="session not found")
    _session_or_not_found(request, session_id)

    def _work() -> dict[str, Any]:
        try:
            return portable.build_set_document(
                session_id,
                sets_root=_sets_root(request),
                track_lookup=_library_track_lookup(request),
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        except portable.SessionRecording as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": exc.message},
            ) from exc

    document = await run_in_threadpool(_work)
    headers = {"Content-Disposition": f'attachment; filename="{session_id}.set.json"'}
    return JSONResponse(document, headers=headers)


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

    try:
        TimelineJsonl(jsonl).validate()
    except TimelineCorruptionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    hidden = _hidden_stable_ids_for_request(request)
    audience = getattr(request.state, "share_audience", "local")

    if audience != "share" and not hidden:
        def _stream() -> Iterable[bytes]:
            with jsonl.open("rb") as fh:
                yield from fh

        return StreamingResponse(_stream(), media_type="application/x-ndjson")

    def _filtered_stream() -> Iterable[bytes]:
        """Share and local overlays both redact hidden tracks (NOWP-11487)."""
        nonblank = [
            line
            for line in jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        last_index = len(nonblank) - 1
        for index, line in enumerate(nonblank):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                if index < last_index:
                    raise
                continue
            if audience == "share":
                event = _share_timeline_event(parsed, hidden)
                if event is not None:
                    yield json.dumps(event, separators=(",", ":")).encode() + b"\n"
            else:
                yield _redact_timeline_line(line, hidden).encode() + b"\n"

    return StreamingResponse(_filtered_stream(), media_type="application/x-ndjson")


@router.get("/{session_id}/tracklist", response_model=TracklistResponse)
async def api_get_tracklist(request: Request, session_id: str) -> JSONResponse:
    tracklist = await _tracklist_or_error(request, session_id, with_library=True)
    return JSONResponse(tracklist.to_dict())


@router.get(
    "/{session_id}/tracklist.{fmt}",
    response_class=Response,
    responses={
        200: {
            "content": {
                "text/plain": {},
                "text/markdown": {},
                "text/csv": {},
                "application/x-cue": {},
                "audio/x-mpegurl": {},
            }
        }
    },
)
async def api_get_tracklist_export(
    request: Request,
    session_id: str,
    fmt: TracklistFormat,
    acknowledge_rights: bool = Query(False),
) -> Response:
    if fmt not in (*FILE_FORMATS, "txt", "md", "csv", "timestamped", "soundcloud"):
        raise HTTPException(status_code=422, detail="unknown export format")
    if fmt in FILE_FORMATS and getattr(request.state, "share_audience", "local") == "share":
        raise HTTPException(status_code=404, detail="session not found")
    if fmt == "soundcloud" and not acknowledge_rights:
        raise HTTPException(status_code=400, detail=LICENSING_REMINDER)
    with_library = fmt == "m3u8"
    tracklist = await _tracklist_or_error(request, session_id, with_library=with_library)
    body = render_tracklist(tracklist, fmt)
    media = tracklist_media_type(fmt)
    name = tracklist_filename(session_id, fmt).replace('"', "_")
    return Response(
        content=body,
        media_type=f"{media}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


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


@router.post("/{session_id}/split", response_model=SplitResponse)
async def api_split_tracks(
    request: Request,
    session_id: str,
    body: SplitRequest | None = None,
) -> SplitResponse:
    _session_or_not_found(request, session_id)
    out_dir: Path | None = None
    if body is not None and body.out_dir is not None:
        if "\x00" in body.out_dir:
            raise HTTPException(status_code=422, detail="out_dir must be an absolute path")
        candidate = Path(body.out_dir)
        if not candidate.is_absolute():
            raise HTTPException(status_code=422, detail="out_dir must be an absolute path")
        out_dir = candidate
    try:
        files = await run_in_threadpool(
            split_recording,
            session_id,
            sets_root=_sets_root(request),
            out_dir=out_dir,
        )
    except sets_paths.SessionPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    except SplitRefused as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SplitResponse(
        session_id=session_id,
        track_files=[TrackFileResponse(**f.__dict__) for f in files],
    )


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
    "SetImportResponse",
    "SoundcloudExportAckRequest",
    "SoundcloudExportResponse",
    "SplitRequest",
    "SplitResponse",
    "TrackFileResponse",
    "TracklistResponse",
    "TracklistRowModel",
    "router",
]
