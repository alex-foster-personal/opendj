"""rb-assets routes -- the 4 NEW endpoints backing /performance.

Contract: .planning/rekordbox-parity/COMPONENT-MAP.md section 2. All four
are GET-only reads over state.db + data/master.plain.db + on-disk rekordbox
share assets; resolution lives in :mod:`apps.webui.server.rb_vendor`.

The integrator wires ``router`` into ``create_app()`` under ``/api/v1``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_read_state
from ..models import QualityOut
from ..rb_vendor_pkg import own_beatgrid_overlay

router = APIRouter(prefix="/tracks", tags=["rb-assets"])

_CACHE_AUDIO = "no-store"  # files can move (apps/reconcile repairs)
_CACHE_ARTWORK = "public, max-age=86400"
# REVALIDATE, never serve from cache blind. This payload varies with two
# things the URL does not name: whether an own record has landed for the track
# (a background backfill can promote one at any moment) and the PARITY-02
# source selection, a process-local toggle. Under the previous
# `public, max-age=3600` a browser replayed the old grid for up to an hour
# after either changed, and the earlier fix only sent `no-store` once the
# response had ALREADY resolved to `own`, so a response cached before the
# promotion never reached the server to be corrected (Codex P1 BLOCKING,
# PR #1587). `no-cache` still lets the browser HOLD the body; it just has to
# ask first, and the ETag below turns that ask into a 304 for the common
# unchanged case rather than 1.2 MB on every deck load.
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


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """Return whether an If-None-Match value weakly matches this GET ETag."""
    if if_none_match is None:
        return False
    return any(
        candidate.strip() == "*" or candidate.strip().removeprefix("W/") == etag
        for candidate in if_none_match.split(",")
    )


def _state_db_override(request: Request) -> Path | None:
    """The app-configured analysis DB path, or None to take the callee's own default.

    Unlike `apps.webui.server.routes.analysis._analysis_db_path`, this never
    substitutes `apps.shared.paths.STATE_DB` for an absent override: the
    own-beatgrid overlay's default is the DISTINCT
    `apps.adapters.rekordbox.config.STATE_DB` constant (the two normally point
    at the same file, but tests monkeypatch them independently), so forcing
    the wrong one here would fix the app-configured-DB case while breaking the
    ordinary default case (Codex P2 BLOCKING, PR #1587).
    """
    override = getattr(request.app.state, "analysis_db_path", None)
    return Path(override) if override is not None else None


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
    state_db_path = _state_db_override(request)
    try:
        content = rb_vendor.resolve_content(stable_id)
        payload = rb_vendor.build_anlz_payload(content, points, state_db_path)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = detail.get("code")
        if code == "VENDOR_MAPPING_NOT_FOUND":
            # Locally imported track (no rekordbox analysis). Everything rekordbox
            # owns stays empty, but the waveform is decodable from the audio
            # itself, so serve OUR peaks (ffmpeg, cached under
            # data/state/local-waveform-cache) and say so in ``local_waveform``.
            # A decode that has not and cannot run yields empty bands plus the
            # reason - never a synthesised shape.
            # Same audience GET /audio resolves for this request, so a Share
            # listener's lane is drawn from the rung they actually hear.
            share = getattr(request.state, "share_audience", "local") == "share"
            payload = rb_vendor.local_anlz_payload(stable_id, points, share=share)
            # The own-beatgrid overlay again, on THIS branch too. `build_anlz_payload`
            # applies it on the mapped branch, but a locally imported file never goes
            # through that function, so a track with a canonical own record was still
            # served an empty rekordbox-labelled grid under `beatgrid=own` - and a
            # local import is exactly the track most likely to have no rekordbox
            # analysis and most likely to depend on ours (Codex P1 BLOCKING,
            # PR #1587). Applied here rather than inside `local_anlz_payload`, which
            # belongs to the waveform lane; this route already owns choosing between
            # the two branches.
            payload = own_beatgrid_overlay.apply_own_beatgrid(payload, stable_id, state_db_path)
        elif code == "ANALYSIS_NOT_FOUND":
            # A MAPPED track whose vendor ANLZ files are missing, unsafe, or
            # wholly unparseable. `build_anlz_payload` raised before the own
            # overlay ever ran, so a track with the beatgrid lane promoted to
            # own and a valid canonical own record could not be served at all
            # (Codex P1 BLOCKING, PR #1587) even though the own lane has a
            # real, honest answer independent of the vendor's broken file.
            #
            # Cues live in djmdCue, not the ANLZ files, so they are still real
            # data for this track and are fetched for real rather than reused
            # from `empty_anlz_payload`'s local-import stub.
            payload = rb_vendor.empty_anlz_payload(stable_id, points)
            payload["cues"] = rb_vendor.fetch_cues(content.vendor_id)
            payload = own_beatgrid_overlay.apply_own_beatgrid(payload, stable_id, state_db_path)
            if payload["beatgrid"]["source"] != own_beatgrid_overlay.SOURCE_OWN:
                # The beatgrid lane is not on own, so there is nothing real to
                # show: the rekordbox stub in `payload` would be
                # indistinguishable from a genuinely silent, healthy track
                # (GUARD-01 H10). Keep failing loud rather than paint over a
                # dead vendor source.
                raise
        else:
            raise
    local_waveform = payload.get("local_waveform")
    retryable = isinstance(local_waveform, dict) and local_waveform.get("retryable") is True
    # An own-sourced beatgrid is NOT publicly cacheable for an hour. This
    # response varies with two things the URL does not name: whether an own
    # record has landed for the track, and the PARITY-02 source selection, which
    # is a process-local toggle that can flip mid-session. With `max-age=3600` a
    # browser keeps replaying the old grid for up to an hour after either
    # changes, and no ETag exists on this endpoint to revalidate against
    # (Codex P1 BLOCKING, PR #1587). The rekordbox-sourced path is unchanged: it
    # varies only with the ANLZ files, which the file cache already keys on.
    if retryable:
        # A retryable miss is a statement about this MOMENT, not about the
        # track, so it is not held at all, with or without revalidation.
        return JSONResponse(payload, headers={"Cache-Control": _CACHE_ANLZ_RETRYABLE})

    # The ETag is the digest of the response itself, so it cannot disagree
    # with what it labels: any change to the grid, its source, or the waveform
    # changes the body and therefore the tag. Deriving it from inputs instead
    # (ANLZ mtimes plus the record digest plus the selection) would be faster
    # and would be one more thing to keep in sync with the payload.
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    etag = f'"{hashlib.sha256(body.encode("utf-8")).hexdigest()}"'
    headers = {"Cache-Control": _CACHE_ANLZ, "ETag": etag}
    if _if_none_match_hits(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def _if_none_match_hits(header: str | None, etag: str) -> bool:
    """True when the client already holds this exact representation.

    RFC 9110: the header is a comma-separated LIST, may be `*`, and each member
    may carry the `W/` weak prefix. A bare `header == etag` comparison misses
    every one of those, and a miss here is not a visible failure - it just
    quietly sends 1.2 MB that did not need sending, which is why it would
    never be noticed.
    """
    if not header:
        return False
    candidates = [part.strip() for part in header.split(",")]
    if "*" in candidates:
        return True
    return any(part.removeprefix("W/") == etag for part in candidates)


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
