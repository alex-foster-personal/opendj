"""rb-assets routes -- the 4 NEW endpoints backing /performance.

Contract: .planning/rekordbox-parity/COMPONENT-MAP.md section 2. All four
are GET-only reads over state.db + data/master.plain.db + on-disk rekordbox
share assets; resolution lives in :mod:`apps.webui.server.rb_vendor`.

The integrator wires ``router`` into ``create_app()`` under ``/api/v1``.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_read_state
from ..models import QualityOut

router = APIRouter(prefix="/tracks", tags=["rb-assets"])

_CACHE_AUDIO = "no-store"  # files can move (apps/reconcile repairs)
_CACHE_ARTWORK = "public, max-age=86400"
_CACHE_ANLZ = "public, max-age=3600"
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
    """COMPONENT-MAP 2.4 response (+ duration_s / comment per build brief)."""

    stable_id: str
    vendor: str
    vendor_id: str
    folder_path: Optional[str]
    file_exists: bool
    is_streaming: bool
    genre: Optional[str]
    comment: Optional[str]
    duration_s: Optional[int]
    artwork_available: bool
    analysis_available: bool
    beatgrid_issue: Optional[BeatgridIssueOut]
    cue_count: int
    quality: QualityOut


@router.get("/{stable_id}/audio", response_class=FileResponse)
def get_track_audio(
    stable_id: str,
    _backend: StateBackend = Depends(get_read_state),
) -> FileResponse:
    """Stream the local audio file. FileResponse handles Range/206 natively.

    Rekordbox tracks resolve via the vendor mapping; locally imported tracks
    (no djmdContent row, e.g. vocal stems) fall back to ``tracks.file_path``.
    """
    try:
        content = rb_vendor.resolve_content(stable_id)
        path, media_type = rb_vendor.audio_file(content)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") != "VENDOR_MAPPING_NOT_FOUND":
            raise
        path, media_type = rb_vendor.local_audio_file(stable_id)
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": _CACHE_AUDIO},
    )


@router.get("/{stable_id}/artwork", response_class=FileResponse)
def get_track_artwork(
    stable_id: str,
    size: Literal["s", "m", "orig"] = Query(
        "s", description="s=80x80 browser rows, m=240x240 deck thumbs, orig"
    ),
    _backend: StateBackend = Depends(get_read_state),
) -> FileResponse:
    """Serve the rekordbox artwork jpg at the requested size variant."""
    content = rb_vendor.resolve_content(stable_id)
    path = rb_vendor.artwork_file(content, size)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": _CACHE_ARTWORK},
    )


@router.get("/{stable_id}/anlz")
def get_track_anlz(
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
    content = rb_vendor.resolve_content(stable_id)
    payload = rb_vendor.build_anlz_payload(content, points)
    return JSONResponse(payload, headers={"Cache-Control": _CACHE_ANLZ})


@router.get("/{stable_id}/rb-meta", response_model=RbMetaOut)
def get_track_rb_meta(
    stable_id: str,
    response: Response,
    _backend: StateBackend = Depends(get_read_state),
) -> RbMetaOut:
    """Rekordbox vendor fields + file-existence flags for browser rows."""
    content = rb_vendor.resolve_content(stable_id)
    response.headers["Cache-Control"] = _CACHE_RB_META
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
