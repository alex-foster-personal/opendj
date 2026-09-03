"""rb-assets routes -- the 4 NEW endpoints backing /performance.

Contract: .planning/rekordbox-parity/COMPONENT-MAP.md section 2. All four
are GET-only reads over state.db + data/master.plain.db + on-disk rekordbox
share assets; resolution lives in :mod:`apps.webui.server.rb_vendor`.

The integrator wires ``router`` into ``create_app()`` under ``/api/v1``.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from apps.adapters.rekordbox.paths import empty_anlz_payload

from .. import rb_vendor
from ..analysis_source import DEFAULT_SOURCE, AnalysisSourceStore
from ..backend import StateBackend
from ..deps import get_read_state
from ..models import QualityOut
from . import analysis as analysis_routes

router = APIRouter(prefix="/tracks", tags=["rb-assets"])

_CACHE_AUDIO = "no-store"  # files can move (apps/reconcile repairs)
_CACHE_ARTWORK = "public, max-age=86400"
_CACHE_ANLZ = "public, max-age=3600"
_CACHE_ANLZ_RETRYABLE = "no-store"  # transient decoder saturation, not a fact about the track
_CACHE_RB_META = "no-store"  # file_exists must reflect disk truth


class BeatgridIssueOut(BaseModel):
    """A real, already-detected PQTZ field-vs-interval BPM disagreement.

    See ``rb_vendor.cached_beatgrid_issue`` for the caching tradeoff: this is
    None both when the grid is clean AND when it has never been evaluated
    yet - never a guessed verdict.
    """

    severity: Literal["warning", "error"]
    at_sec: float
    field_bpm: float
    interval_bpm: float
    disagreement_bpm: float


class RbMetaOut(BaseModel):
    """COMPONENT-MAP 2.4 response (+ duration_s / comment per build brief).

    ``vendor`` is ``local`` for a track with no rekordbox vendor mapping: the
    rekordbox-sourced fields are then honestly empty (``vendor_id`` None,
    analysis False, no cues, no genre) while ``folder_path``, ``file_exists``
    and ``quality`` still carry the state layer's own disk truth.
    ``artwork_available`` is the one exception -- it reflects a real embedded
    tag picture on the local file when present, since ``/artwork`` now
    serves that instead of a rekordbox-rendered jpg for these rows. See
    :func:`_local_rb_meta`.

    ``artwork_available`` is tri-state (``bool | None``) rather than a plain
    bool: for a local-vendor row it is ``None`` when the optional ``mutagen``
    tag reader was never available to check with, distinct from a checked
    ``False`` (no embedded picture). A rekordbox-vendor row never needs the
    reader, so it is always a definite ``True``/``False`` there. See
    :func:`apps.adapters.rekordbox.paths.local_artwork_available` (#795).
    """

    stable_id: str
    vendor: Literal["rekordbox", "local"]
    vendor_id: str | None
    folder_path: str | None
    file_exists: bool
    is_streaming: bool
    genre: str | None
    comment: str | None
    duration_s: int | None
    artwork_available: bool | None
    analysis_available: bool
    beatgrid_issue: BeatgridIssueOut | None
    cue_count: int
    quality: QualityOut


@router.get("/{stable_id}/audio", response_class=FileResponse)
def get_track_audio(
    stable_id: str,
    request: Request,
    _backend: StateBackend = Depends(get_read_state),
) -> FileResponse:
    """Stream the single best working file. FileResponse handles Range/206.

    The backend picks among ``track_locations`` plus the legacy
    ``file_path`` / FolderPath. The frontend never sees the alternatives.
    Share-host requests use the share venue cap (lossy ceiling by default).
    """
    share = getattr(request.state, "share_audience", "local") == "share"
    picked = rb_vendor.resolve_playable_audio(stable_id, share=share)
    return FileResponse(
        picked.path,
        media_type=picked.media_type,
        headers={
            "Cache-Control": _CACHE_AUDIO,
            "X-Audio-Kind": picked.kind,
            "X-Audio-Venue": picked.venue_key or "",
            "X-Audio-Source": picked.source,
        },
    )


@router.get("/{stable_id}/artwork", response_class=FileResponse, response_model=None)
def get_track_artwork(
    stable_id: str,
    size: Literal["s", "m", "orig"] = Query(
        "s", description="s=80x80 browser rows, m=240x240 deck thumbs, orig"
    ),
    _backend: StateBackend = Depends(get_read_state),
) -> FileResponse | Response:
    """Serve artwork for the track: rekordbox's pre-rendered jpg variant when
    mapped, else the embedded tag picture read straight from the local file.

    The embedded-tag path has no pre-rendered s/m/orig variants (rekordbox
    never touched this file), so ``size`` is not honoured there -- the real
    embedded image is served at its original dimensions and mime type for
    all three, rather than fabricating a resize.
    """
    try:
        content = rb_vendor.resolve_content(stable_id)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") != "VENDOR_MAPPING_NOT_FOUND":
            raise
        data, mime = rb_vendor.local_artwork(stable_id)
        return Response(
            content=data,
            media_type=mime,
            headers={"Cache-Control": _CACHE_ARTWORK},
        )
    path = rb_vendor.artwork_file(content, size)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": _CACHE_ARTWORK},
    )


def _current_beatgrid_source(request: Request) -> str:
    """The PARITY-02 rbx-vs-own selection currently in effect for beatgrids.

    ``app.state.analysis_source`` is set by :func:`apps.webui.server.app.create_app`;
    a test app that mounts only this router (same convention as
    ``app.state.analysis_db_path`` in ``routes.analysis``) gets the
    documented ``"rekordbox"`` default rather than an AttributeError.
    """
    store: AnalysisSourceStore | None = getattr(request.app.state, "analysis_source", None)
    return store.get("beatgrid") if store is not None else DEFAULT_SOURCE


def _resolve_beatgrid_source(request: Request, stable_id: str, payload: dict) -> None:
    """Mutate ``payload`` per the PARITY-02 rbx-vs-own selection (in place).

    A value-only swap, never a schema branch: ``"own"`` replaces
    ``payload["beatgrid"]`` with the apps.analysis-derived grid in the
    identical ``/anlz`` shape (:func:`analysis_routes.synthesize_fallback_beats`,
    the same function ``/beatgrid-fallback`` already serves from). When no
    own analysis exists for this track, the grid goes explicitly empty with
    a stated reason -- it is never silently served the rekordbox grid while
    still claiming ``"own"``.
    """
    source = _current_beatgrid_source(request)
    payload["beatgrid_source"] = source
    if source == "rekordbox":
        payload["beatgrid_own_unavailable_reason"] = None
        return
    db_path = analysis_routes._analysis_db_path(request)
    record = analysis_routes._load_latest_record(db_path, stable_id, None)
    beats = analysis_routes.synthesize_fallback_beats(record) if record is not None else None
    if record is None or beats is None:
        payload["beatgrid"] = {"beat_count": 0, "beats": []}
        payload["beatgrid_own_unavailable_reason"] = (
            "no own analysis for this track"
            if record is None
            else "own analysis has no usable downbeats"
        )
        return
    payload["beatgrid"] = {
        "beat_count": len(beats),
        "beats": [beat.model_dump() for beat in beats],
    }
    payload["beatgrid_own_unavailable_reason"] = None


@router.get("/{stable_id}/anlz")
def get_track_anlz(
    request: Request,
    stable_id: str,
    points: int = Query(
        38400,
        ge=100,
        le=38400,
        description="Max length of each waveform band array after downsampling",
    ),
    _backend: StateBackend = Depends(get_read_state),
) -> JSONResponse:
    """Waveform (preview + detail) / beatgrid / cues / phrases JSON.

    The ``vocals`` field carries FOUR statuses: ``rekordbox`` (PVDI),
    ``no_vocals`` (PVDI present, all zero), ``demucs`` (local detection
    from data/state/vocal-cache, merged when PVDI is absent), and
    ``not_analyzed`` (NEITHER source exists).
    """
    try:
        content = rb_vendor.resolve_content(stable_id)
        payload = rb_vendor.build_anlz_payload(content, points)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = detail.get("code")
        if code == "VENDOR_MAPPING_NOT_FOUND":
            # Locally imported track (no rekordbox analysis). Everything
            # rekordbox owns stays empty, but the waveform is decodable from
            # the audio itself, so serve OUR peaks (ffmpeg, cached under
            # data/state/local-waveform-cache) and say so in
            # ``local_waveform``. A decode that has not and cannot run
            # yields empty bands plus the reason - never a synthesised shape.
            # Same audience GET /audio resolves for this request, so a Share
            # listener's lane is drawn from the rung they actually hear.
            share = getattr(request.state, "share_audience", "local") == "share"
            payload = rb_vendor.local_anlz_payload(stable_id, points, share=share)
        elif code == "ANALYSIS_NOT_FOUND" and _current_beatgrid_source(request) == "own":
            # The track IS rekordbox-mapped, but its ANLZ directory is
            # missing, unsafe, or wholly unparseable -- ordinarily a hard
            # 404. OWN is selected, though, and an own-rolled apps.analysis
            # record can exist for this stable_id regardless of whether
            # rekordbox's own analysis is readable (the same cohort
            # /beatgrid-fallback already serves). Build the same honest
            # empty base payload the no-mapping branch above uses, so
            # `_resolve_beatgrid_source` gets a chance to overlay the real
            # OWN grid instead of the whole request dying before OWN is
            # even consulted.
            payload = empty_anlz_payload(stable_id, points)
        else:
            raise
    _resolve_beatgrid_source(request, stable_id, payload)
    local_waveform = payload.get("local_waveform")
    retryable = isinstance(local_waveform, dict) and local_waveform.get("retryable") is True
    cache_control = _CACHE_ANLZ_RETRYABLE if retryable else _CACHE_ANLZ
    return JSONResponse(payload, headers={"Cache-Control": cache_control})


def _local_rb_meta(stable_id: str) -> RbMetaOut:
    """rb-meta for a track with NO rekordbox vendor mapping - never a 404.

    Mirrors :func:`get_track_anlz`'s VENDOR_MAPPING_NOT_FOUND branch: every
    rekordbox-sourced field is empty because it genuinely does not exist for a
    locally imported file, and nothing is synthesised to fill the gap. The
    fields that are NOT rekordbox facts - file_exists, quality, and
    artwork_available (an embedded tag, not a rekordbox render) - are still
    measured, from the same state-layer file_path and the same cached stat the
    bulk listing uses, so a row and its rb-meta cannot disagree.
    """
    file_path, duration_ms = rb_vendor.local_track_row(stable_id)
    quality = rb_vendor.bulk_quality(
        [stable_id], {stable_id: file_path}, {stable_id: duration_ms}
    )[stable_id]
    return RbMetaOut(
        stable_id=stable_id,
        vendor="local",
        vendor_id=None,
        folder_path=file_path,
        file_exists=rb_vendor.bulk_availability(
            [stable_id], {stable_id: file_path}, {}
        )[stable_id],
        is_streaming=rb_vendor.is_streaming_path(file_path),
        genre=None,
        comment=None,
        duration_s=duration_ms // 1000 if duration_ms is not None else None,
        artwork_available=rb_vendor.local_artwork_available(file_path),
        analysis_available=False,
        beatgrid_issue=None,
        cue_count=0,
        quality=QualityOut(**quality),
    )


@router.get("/{stable_id}/rb-meta", response_model=RbMetaOut)
def get_track_rb_meta(
    stable_id: str,
    response: Response,
    _backend: StateBackend = Depends(get_read_state),
) -> RbMetaOut:
    """Rekordbox vendor fields + file-existence flags for browser rows."""
    response.headers["Cache-Control"] = _CACHE_RB_META
    try:
        content = rb_vendor.resolve_content(stable_id)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") != "VENDOR_MAPPING_NOT_FOUND":
            raise
        # Locally imported track: empty-but-valid payload, same contract shape
        # as get_track_anlz's fallback. A whole locally-imported library would
        # otherwise 404 once per visible row (console noise, no information).
        return _local_rb_meta(stable_id)
    is_streaming = rb_vendor.is_streaming_path(content.folder_path)
    # Same residency gate as bulk listings (dataless stubs == missing).
    file_exists = False
    if content.folder_path is not None and not is_streaming:
        sizes = rb_vendor.bulk_file_size([content.folder_path])
        file_exists = sizes.get(content.folder_path) is not None
    artwork_available = (
        content.image_path is not None
        and rb_vendor.resolve_share_path(content.image_path).is_file()
    )
    analysis_available = (
        content.analysis_data_path is not None
        and rb_vendor.resolve_share_path(content.analysis_data_path).is_file()
    )
    beatgrid_issue = rb_vendor.cached_beatgrid_issue(content)
    # One track, so a direct stat is fine -- and it reuses the bulk cache,
    # which the listing has usually already warmed for this path.
    quality = rb_vendor.bulk_quality(
        [content.stable_id],
        {content.stable_id: content.folder_path},
        {content.stable_id: (content.length_s or 0) * 1000 or None},
    )[content.stable_id]
    return RbMetaOut(
        stable_id=content.stable_id,
        vendor="rekordbox",
        vendor_id=content.vendor_id,
        folder_path=content.folder_path,
        file_exists=file_exists,
        is_streaming=is_streaming,
        genre=content.genre,
        comment=content.comment,
        duration_s=content.length_s,
        artwork_available=artwork_available,
        analysis_available=analysis_available,
        beatgrid_issue=BeatgridIssueOut(**beatgrid_issue) if beatgrid_issue is not None else None,
        cue_count=rb_vendor.count_cues(content.vendor_id),
        quality=QualityOut(**quality),
    )
