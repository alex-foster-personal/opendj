"""rb-assets routes -- the 4 NEW endpoints backing /performance.

Contract: .planning/rekordbox-parity/COMPONENT-MAP.md section 2. All four
are GET-only reads over state.db + data/master.plain.db + on-disk rekordbox
share assets; resolution lives in :mod:`apps.webui.server.rb_vendor`.

The integrator wires ``router`` into ``create_app()`` under ``/api/v1``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from apps.adapters.rekordbox.paths import empty_anlz_payload
from apps.analysis import canonical, selection
from apps.analysis.record import AnalysisRecord

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_read_state
from ..models import QualityOut
from . import analysis as analysis_routes
from . import analysis_source as analysis_source_routes

router = APIRouter(prefix="/tracks", tags=["rb-assets"])

_CACHE_AUDIO = "no-store"  # files can move (apps/reconcile repairs)
_CACHE_ARTWORK = "public, max-age=86400"
# `private, no-cache`, NOT `public, max-age=3600`, since PARITY-02: the body
# depends on the process-local rbx-vs-own beatgrid toggle
# (`_resolve_beatgrid_source` below), which an agent can flip over HTTP at any
# moment with no change to this URL. A shared cache could then serve one
# audience's OWN grid to another, and a plain browser navigation could keep
# serving a pre-switch RBX grid for an hour (discussion_r3970967302). The app
# is insulated by `gen` (anlz-fetch-generation.ts) but a direct HTTP consumer
# is not, and this endpoint is agent-facing. `no-cache` means "store it, but
# revalidate every time", so the ETag below still keeps the multi-MB body off
# the wire when nothing changed - the freshness rule tightens, the bytes do
# not move.
_CACHE_ANLZ = "private, no-cache"
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
    analysis False and no cues). Genre and comment instead come from the
    state layer's import-time file tags, alongside ``folder_path``,
    ``file_exists`` and ``quality``.
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
    request: Request,
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
        etag = f'"{hashlib.sha256(data).hexdigest()}"'
        headers = {"Cache-Control": _CACHE_ARTWORK, "ETag": etag}
        if _etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        return Response(
            content=data,
            media_type=mime,
            headers=headers,
        )
    path = rb_vendor.artwork_file(content, size)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": _CACHE_ARTWORK},
    )


# The `/anlz` wire contract's `beatgrid_source` predates PARITY-02's lane
# selection module and uses "rekordbox"/"own"; `apps.analysis.selection`
# uses "rbx"/"own". Translate at the boundary rather than widen the wire
# vocabulary to match an internal module.
_BEATGRID_SOURCE_LABELS: dict[str, str] = {"rbx": "rekordbox", "own": "own"}


def _current_beatgrid_source(request: Request) -> str:
    """The PARITY-02 rbx-vs-own selection currently in effect for beatgrids.

    Reads through :mod:`apps.analysis.selection` (persisted default plus
    in-memory dev toggle), via the same read-only, missing-file-safe
    connection its own ``GET /api/v1/analysis/source`` endpoint uses -- a
    test app that mounts only this router still gets the documented
    ``"rekordbox"`` default rather than a 500 on a fresh daemon.
    """
    conn = analysis_source_routes._open_ro(request)
    try:
        source = selection.effective_source(conn, "beatgrid")
    finally:
        conn.close()
    return _BEATGRID_SOURCE_LABELS[source]


def _canonical_beatgrid_record(db_path: Path, stable_id: str) -> AnalysisRecord | None:
    """The analysis row ``analysis_canonical`` names for this track's beatgrid.

    THE POINTER, NOT THE NEWEST ROW. ``_load_latest_record(.., None)``
    cannot answer this: its default query excludes every ``own_*`` backend
    on purpose (``analysis.py``), so it returns a legacy librosa row and the
    OWN grid would be paired with the canonical record's BPM -- because
    ``GET /tracks/{id}`` reads ``bpm`` from ``analysis_projection``, which
    :func:`apps.analysis.canonical.rebuild_projection` derives from THIS
    pointer. Two readers of one lane must resolve the same record or the
    read model reports a grid and a tempo that were never measured together
    (discussion_r3969942709 P1 BLOCKING).

    Returns None when the track has no canonical own beatgrid (no pointer,
    or the analysis tables have never been created). A pointer that names a
    row which is not there is corruption, not an absence, and raises.
    """
    conn = analysis_routes._open_analysis_ro(db_path)
    try:
        try:
            pointer = canonical.canonical_pointer(conn, stable_id, "beatgrid")
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
        if pointer is None:
            return None
        backend, backend_version = pointer
        row = conn.execute(
            "SELECT record_json FROM analysis "
            "WHERE stable_id = ? AND backend = ? AND backend_version = ?",
            (stable_id, backend, backend_version),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "ANALYSIS_CANONICAL_DANGLING",
                "message": (
                    f"canonical beatgrid pointer for {stable_id!r} names "
                    f"{backend!r}@{backend_version!r} but no such analysis row exists"
                ),
            },
        )
    return AnalysisRecord.from_json(row[0])


def _own_beatgrid_beats(db_path: Path, stable_id: str) -> tuple[list[dict] | None, str | None]:
    """This track's own beatgrid beats in ``/anlz`` shape, or a stated reason.

    The canonical lane payload's ``beats`` are ALREADY the deck's wire shape
    (``{t, n, bpm}``), validated at the write boundary against the deck's own
    consumer (``apps/analysis/lane_payloads.py`` ``_validate_beats``,
    transcribed from ``beat-sync-math.ts`` ``validateBeatGrid``). They are
    projected key-for-key here, never re-derived: re-synthesising a grid from
    ``downbeats_s`` would interpolate beats the producer did not measure and
    could disagree with the ``bpm`` the same record projects.
    """
    record = _canonical_beatgrid_record(db_path, stable_id)
    if record is None:
        return None, "no own analysis for this track"
    result = record.lanes.get("beatgrid")
    if result is None:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "ANALYSIS_CANONICAL_LANELESS",
                "message": (
                    f"canonical record {record.backend!r} for {stable_id!r} "
                    "carries no beatgrid lane"
                ),
            },
        )
    if result.status != "ok":
        return None, result.reason or f"own beatgrid lane {result.status}"
    beats = [
        {"n": beat["n"], "bpm": beat["bpm"], "t": beat["t"]}
        for beat in result.payload["beats"]
    ]
    return beats, None


def _resolve_beatgrid_source(request: Request, stable_id: str, payload: dict) -> None:
    """Mutate ``payload`` per the PARITY-02 rbx-vs-own selection (in place).

    A value-only swap, never a schema branch: ``"own"`` replaces
    ``payload["beatgrid"]`` with the canonical own beatgrid lane's own beats
    in the identical ``/anlz`` shape (:func:`_own_beatgrid_beats`). When no
    own analysis exists for this track, or its beatgrid lane did not succeed,
    the grid goes explicitly empty carrying that lane's stated reason -- it is
    never silently served the rekordbox grid, nor a legacy non-own grid, while
    still claiming ``"own"``.
    """
    source = _current_beatgrid_source(request)
    payload["beatgrid_source"] = source
    if source == "rekordbox":
        payload["beatgrid_own_unavailable_reason"] = None
        return
    beats, reason = _own_beatgrid_beats(analysis_routes._analysis_db_path(request), stable_id)
    if beats is None:
        payload["beatgrid"] = {"beat_count": 0, "beats": []}
        payload["beatgrid_own_unavailable_reason"] = reason
        return
    payload["beatgrid"] = {"beat_count": len(beats), "beats": beats}
    payload["beatgrid_own_unavailable_reason"] = None


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """Return whether an If-None-Match value weakly matches this GET ETag."""
    if if_none_match is None:
        return False
    return any(
        candidate.strip() == "*" or candidate.strip().removeprefix("W/") == etag
        for candidate in if_none_match.split(",")
    )


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
) -> Response:
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
    if retryable:
        return JSONResponse(payload, headers={"Cache-Control": _CACHE_ANLZ_RETRYABLE})
    # Hashed over the SERIALIZED body, so every input that can change the answer
    # is covered without enumerating them: the beatgrid source and the grid it
    # selects, the vendor ANLZ bytes, the local-waveform decode and `points`.
    # An enumerated key would have to be revisited every time this payload gains
    # a field, and a missed field is a silently stale grid.
    body = json.dumps(payload, separators=(",", ":")).encode()
    etag = f'"{hashlib.sha256(body).hexdigest()}"'
    headers = {"Cache-Control": _CACHE_ANLZ, "ETag": etag}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def _local_rb_meta(stable_id: str) -> RbMetaOut:
    """rb-meta for a track with NO rekordbox vendor mapping - never a 404.

    Mirrors :func:`get_track_anlz`'s VENDOR_MAPPING_NOT_FOUND branch: every
    rekordbox-sourced field is empty because it genuinely does not exist for a
    locally imported file, and nothing is synthesised to fill the gap. The
    fields that are NOT rekordbox facts - genre and comment (the file's own
    tags, kept by the folder import), file_exists, quality, and
    artwork_available (an embedded tag, not a rekordbox render) - are still
    served, from the same state-layer file_path and the same cached stat the
    bulk listing uses, so a row and its rb-meta cannot disagree.

    Every field here resolves through ``rb_vendor`` against one state.db, not
    through the request's ``StateBackend``: a local-only library commonly has
    no backend-visible track at all, and splitting the reads across two stores
    is what made this branch raise NotFoundError instead of answering.
    """
    file_path, duration_ms = rb_vendor.local_track_row(stable_id)
    genre, comment = rb_vendor.local_track_file_tags(stable_id)
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
        genre=genre,
        comment=comment,
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
