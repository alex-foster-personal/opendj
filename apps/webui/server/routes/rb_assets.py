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

from apps.adapters.rekordbox import config as rb_config
from apps.analysis import canonical
from apps.analysis.record import AnalysisRecord
from apps.analysis_structure.serve import merge_own_phrases
from apps.shared import platform_paths, runtime_policy

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_read_state
from ..models import QualityOut
from ..rb_vendor_pkg import own_beatgrid_overlay
from ..rb_vendor_pkg.anlz_missing_analysis import (
    apply_own_beatgrid_when_vendor_absent,
    vendor_analysis_path_absent,
)
from ..rb_vendor_pkg.own_overlays import apply_own_overlays
from . import analysis as analysis_routes
from .rb_assets_beatgrid_source import beatgrid_source_for_track

router = APIRouter(prefix="/tracks", tags=["rb-assets"])

_CACHE_AUDIO = "no-store"  # files can move (apps/reconcile repairs)
_CACHE_ARTWORK = "public, max-age=86400"
# REVALIDATE, never serve from cache blind: `private, no-cache`, NOT
# `public, max-age=3600`. This payload varies with things the URL does not
# name - the process-local PARITY-02 rbx-vs-own toggle
# (`_resolve_beatgrid_source` below), which an agent can flip over HTTP at any
# moment, AND whether an own record has landed for the track (a background
# backfill can promote one at any moment). Under the old `max-age=3600` a
# shared cache or plain browser navigation could keep serving a pre-switch or
# pre-promotion grid for up to an hour (discussion_r3970967302; Codex P1
# BLOCKING, PR #1587), and an earlier fix that only sent `no-store` once a
# response had ALREADY resolved to `own` never reached a response cached
# before the promotion. The app is insulated by `gen`
# (anlz-fetch-generation.ts) but a direct HTTP consumer is not, and this
# endpoint is agent-facing. `no-cache` still lets the browser HOLD the body;
# it just has to ask first, and the ETag below turns that ask into a 304 for
# the common unchanged case rather than resending the multi-MB body on every
# deck load.
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


@router.get("/{stable_id}/artwork", response_class=FileResponse, response_model=None)
def get_track_artwork(
    request: Request,
    stable_id: str,
    size: Literal["s", "m", "orig"] = Query(
        "s", description="s=80x80 browser rows, m=240x240 deck thumbs, orig"
    ),
    online: bool = Query(
        False, description="also try MusicBrainz + Cover Art Archive (decks only)"
    ),
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> FileResponse | Response:
    """Serve the track's artwork from the first source that has one.

    rekordbox's pre-rendered jpg variant, else the embedded picture, a cover
    image beside the file, or a cached online cover (looked up only with
    ``online=true``); see :func:`apps.adapters.rekordbox.paths.local_artwork`.
    ``size`` applies to the rekordbox variants only; others are served as found.
    """
    try:
        content = rb_vendor.resolve_content(stable_id)
    except HTTPException as exc:
        if _error_code(exc) != "VENDOR_MAPPING_NOT_FOUND":
            raise
        return _local_artwork_response(request, stable_id, online=online)
    try:
        path = rb_vendor.artwork_file(content, size)
    except HTTPException as exc:
        if _error_code(exc) != "ARTWORK_NOT_FOUND":
            raise
        # rekordbox holds no artwork for this track on this machine (no
        # ImagePath, or its Artwork folder is elsewhere): the rest of the chain.
        return _local_artwork_response(request, stable_id, online=online)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": _CACHE_ARTWORK},
    )


def _error_code(exc: HTTPException) -> str | None:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    return detail.get("code")


def _local_artwork_response(request: Request, stable_id: str, *, online: bool) -> Response:
    data, mime = rb_vendor.local_artwork(stable_id, online=online)
    etag = f'"{hashlib.sha256(data).hexdigest()}"'
    headers = {"Cache-Control": _CACHE_ARTWORK, "ETag": etag}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type=mime, headers=headers)


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
        # Same string apply_own_beatgrid publishes: two paths, one reason.
        return None, own_beatgrid_overlay.OWN_BEATGRID_MISSING_REASON
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


def _resolve_beatgrid_source(source: str, db_path: Path, stable_id: str, payload: dict) -> None:
    """Stamp ``payload["beatgrid_source"]`` per the PARITY-02 rbx-vs-own
    selection (in place), and rebuild ``payload["beatgrid"]`` ONLY when the
    own-beatgrid overlay (``own_beatgrid_overlay.apply_own_beatgrid``,
    PR #1587) has not already done so for this same selection.

    That overlay already ran, inside ``build_anlz_payload`` for the success
    path or explicitly in the exception branches above, using its own
    ``state_db_path``-scoped connection and the richer ``{source, status,
    reason, beat_count, beats, ...}`` shape every other own-record consumer
    (own_beatgrid_overlay.py, the frontend's AnlzData) now expects. Rebuilding
    it here unconditionally with the OLDER ``{beat_count, beats}`` shape this
    function used before that overlay existed would silently drop the
    ``source``/``status``/``reason`` fields the overlay just installed -- this
    checks `payload["beatgrid"].get("source") == "own"` first and leaves an
    already-correct own block alone.

    The independent lookup below (:func:`_own_beatgrid_beats`, against
    ``db_path`` = ``analysis_routes._analysis_db_path(request)``) is the
    FALLBACK for when the overlay's own ``state_db_path`` read did not land
    on "own" -- the two can legitimately point at different databases (see
    `_state_db_override`'s docstring) -- so this selection still gets a
    chance to serve the real own grid rather than the whole request dying or
    silently staying on rekordbox.

    ``source`` is a SNAPSHOT the caller already took, not re-read here: the
    caller decides between two branches (the ANALYSIS_NOT_FOUND rescue above,
    and this function's own own-vs-rekordbox branch) off what must be the
    SAME read, or an agent's PUT landing between two independent
    `_current_beatgrid_source` calls can rescue a 404 into an empty payload
    stamped `rekordbox`, a permanent lying 200 stable under its own ETag
    (discussion_r3974235458 P2 BLOCKING).
    """
    payload["beatgrid_source"] = source
    grid = payload.get("beatgrid") or {}
    if source == "rekordbox":
        payload["beatgrid_own_unavailable_reason"] = None
        return
    if grid.get("source") == own_beatgrid_overlay.SOURCE_OWN:
        # Already resolved by apply_own_beatgrid in the richer shape -- just
        # mirror its reason onto the legacy field, do not rebuild the block.
        payload["beatgrid_own_unavailable_reason"] = grid.get("reason")
        return
    beats, reason = _own_beatgrid_beats(db_path, stable_id)
    if beats is None:
        payload["beatgrid"] = {
            "source": own_beatgrid_overlay.SOURCE_OWN,
            "status": "missing",
            "reason": reason,
            "beat_count": 0,
            "beats": [],
        }
        payload["beatgrid_own_unavailable_reason"] = reason
        return
    payload["beatgrid"] = {
        "source": own_beatgrid_overlay.SOURCE_OWN,
        "status": "ok",
        "reason": None,
        "beat_count": len(beats),
        "beats": beats,
    }
    payload["beatgrid_own_unavailable_reason"] = None


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
        runtime_policy.ANLZ_POINTS_DEFAULT,
        ge=runtime_policy.ANLZ_POINTS_MIN,
        le=runtime_policy.ANLZ_POINTS_MAX,
        description="Max length of each waveform band array after downsampling",
    ),
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> Response:
    """Waveform (preview + detail) / beatgrid / cues / phrases JSON.

    The ``vocals`` field carries FOUR statuses: ``rekordbox`` (PVDI),
    ``no_vocals`` (PVDI present, all zero), ``demucs`` (local detection
    from data/state/vocal-cache, merged when PVDI is absent), and
    ``not_analyzed`` (NEITHER source exists).
    """
    # Read ONCE and thread the snapshot through both decisions below: the
    # ANALYSIS_NOT_FOUND rescue and _resolve_beatgrid_source's own branch
    # must agree on the same selection, or an agent's PUT landing between
    # two independent reads can rescue a 404 into an empty payload this
    # function then stamps with the OTHER, newer source
    # (discussion_r3974235458 P2 BLOCKING).
    has_rb_mapping = stable_id in rb_vendor.bulk_rb_meta([stable_id])
    beatgrid_source, beatgrid_source_basis = beatgrid_source_for_track(
        request, has_rb_mapping=has_rb_mapping
    )
    state_db_path = _state_db_override(request)
    rescued_missing_analysis_path = False
    try:
        content = rb_vendor.resolve_content(stable_id)
        payload = rb_vendor.build_anlz_payload(content, points, state_db_path)
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
            # The own-beatgrid overlay again, on THIS branch too. `build_anlz_payload`
            # applies it on the mapped branch, but a locally imported file never goes
            # through that function, so a track with a canonical own record was still
            # served an empty rekordbox-labelled grid under `beatgrid=own` - and a
            # local import is exactly the track most likely to have no rekordbox
            # analysis and most likely to depend on ours (Codex P1 BLOCKING,
            # PR #1587). Applied here rather than inside `local_anlz_payload`, which
            # belongs to the waveform lane; this route already owns choosing between
            # the two branches.
            payload = apply_own_overlays(
                payload, stable_id, state_db_path, has_rb_mapping=has_rb_mapping
            )
        elif code == "ANALYSIS_NOT_FOUND":
            if vendor_analysis_path_absent(content):
                rescued_missing_analysis_path = True
                share = getattr(request.state, "share_audience", "local") == "share"
                payload = rb_vendor.local_anlz_payload(stable_id, points, share=share)
                payload["cues"] = rb_vendor.fetch_cues(content.vendor_id)
                payload = apply_own_overlays(
                    payload, stable_id, state_db_path, has_rb_mapping=has_rb_mapping
                )
                payload = apply_own_beatgrid_when_vendor_absent(
                    payload, stable_id, state_db_path
                )
            else:
                # A MAPPED track whose vendor ANLZ files are missing, unsafe, or
                # wholly unparseable -- ordinarily a hard 404. `build_anlz_payload`
                # raised before the own overlay ever ran, so a track with the
                # beatgrid lane promoted to own and a valid canonical own record
                # could not be served at all (Codex P1 BLOCKING, PR #1587) even
                # though the own lane has a real, honest answer independent of
                # the vendor's broken file.
                #
                # Cues live in djmdCue, not the ANLZ files, so they are still real
                # data for this track and are fetched for real rather than reused
                # from `empty_anlz_payload`'s local-import stub.
                payload = rb_vendor.empty_anlz_payload(stable_id, points)
                payload["cues"] = rb_vendor.fetch_cues(content.vendor_id)
                payload = apply_own_overlays(
                    payload, stable_id, state_db_path, has_rb_mapping=has_rb_mapping
                )
            if (
                not rescued_missing_analysis_path
                and payload["beatgrid"]["source"] != own_beatgrid_overlay.SOURCE_OWN
                and beatgrid_source != "own"
            ):
                # Neither this DB-level overlay nor the explicit PARITY-02
                # toggle wants own here, so there is nothing real to show:
                # the rekordbox stub in `payload` would be indistinguishable
                # from a genuinely silent, healthy track (GUARD-01 H10). Keep
                # failing loud rather than paint over a dead vendor source.
                # When `beatgrid_source == "own"`, let `_resolve_beatgrid_source`
                # below get a chance to overlay the real OWN grid instead of
                # the whole request dying before OWN is even consulted -- it
                # reads `analysis_routes._analysis_db_path`, a distinct
                # default DB from the one `apply_own_beatgrid` just tried.
                raise
        else:
            raise
    _resolve_beatgrid_source(
        beatgrid_source, analysis_routes._analysis_db_path(request), stable_id, payload
    )
    payload["beatgrid_source_basis"] = beatgrid_source_basis
    # Own sections fill `phrases` only where rekordbox PSSI is empty, on every
    # branch above, and `phrases_source` names which one was served (STRUCT-02).
    payload = merge_own_phrases(payload, stable_id, rb_config.DATA_DIR)
    local_waveform = payload.get("local_waveform")
    retryable = isinstance(local_waveform, dict) and local_waveform.get("retryable") is True
    # Never publicly cacheable for an hour: this response varies with things
    # the URL does not name - the process-local PARITY-02 rbx-vs-own toggle
    # (an agent can flip it over HTTP at any moment) AND whether an own
    # record has landed for the track (a background backfill can promote one
    # at any moment). With the old `public, max-age=3600` a shared cache or
    # plain browser navigation could keep serving a pre-switch or
    # pre-promotion grid for up to an hour (discussion_r3970967302; Codex P1
    # BLOCKING, PR #1587). `no-cache` still lets the browser hold the body;
    # it just has to revalidate first, and the ETag below turns that into a
    # 304 for the common unchanged case rather than resending the multi-MB
    # payload on every deck load.
    if retryable:
        # A retryable miss is a statement about this MOMENT, not about the
        # track, so it is not held at all, with or without revalidation.
        return JSONResponse(payload, headers={"Cache-Control": _CACHE_ANLZ_RETRYABLE})
    # Hashed over the SERIALIZED body, so every input that can change the
    # answer is covered without enumerating them: the beatgrid source and the
    # grid it selects, the vendor ANLZ bytes, the local-waveform decode and
    # `points`. An enumerated key would have to be revisited every time this
    # payload gains a field, and a missed field is a silently stale grid.
    # `sort_keys=True, default=str` keep the digest stable across dict
    # ordering and any non-JSON-native values a source overlay attaches.
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    etag = f'"{hashlib.sha256(body.encode("utf-8")).hexdigest()}"'
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
    file_exists = rb_vendor.bulk_availability(
        [stable_id], {stable_id: file_path}, {}
    )[stable_id]
    return RbMetaOut(
        stable_id=stable_id,
        vendor="local",
        vendor_id=None,
        folder_path=file_path,
        file_exists=file_exists,
        is_streaming=platform_paths.is_streaming_row(
            file_path, file_exists=file_exists
        ),
        genre=genre,
        comment=comment,
        duration_s=duration_ms // 1000 if duration_ms is not None else None,
        artwork_available=rb_vendor.local_artwork_available(stable_id),
        analysis_available=False,
        beatgrid_issue=None,
        cue_count=0,
        quality=QualityOut(**quality),
    )


@router.get("/{stable_id}/rb-meta", response_model=RbMetaOut)
def get_track_rb_meta(
    stable_id: str,
    response: Response,
    _backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
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
    file_exists = rb_vendor.bulk_availability(
        [stable_id],
        {stable_id: content.folder_path},
        {},
    )[stable_id]
    is_streaming = platform_paths.is_streaming_row(
        content.folder_path, file_exists=file_exists
    )
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


from . import rb_assets_audio  # noqa: F401  # registers audio route on router
