"""Tracks endpoints (list / get / patch) -- CAT-05."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from apps.cloud import stem_index
from apps.lyrics import cache as lyrics_cache
from apps.shared import audio_quality
from apps.shared.events import publish
from apps.shared.paths import STATE_DB
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.shared.state.writer_tracks import (
    TrackAlreadyRemovedError,
    TrackLifecycleResult,
    TrackNotFoundError,
    TrackNotRemovedError,
)
from apps.stems.artifacts import DEFAULT_STEMS_DIR, bulk_stem_summaries

from .. import rb_vendor
from ..backend import ConflictError, StateBackend, Track, TrackFilter
from ..deps import get_read_state, get_write_state
from ..errors import precondition_required
from ..etag import compute_etag
from ..models import (
    LyricsUnavailableOut,
    ProvenanceOut,
    QualityRungOut,
    TrackListItemOut,
    TrackLyricsOut,
    TrackOut,
    TrackPatch,
    TrackPlaylistOut,
    TracksPage,
)
from ..rb_vendor_pkg.track_rows import _artwork_facts
from ..reveal_path import RevealPathError, reveal_track_path
from .ingest_job import valid_lyrics_ids

router = APIRouter(prefix="/tracks", tags=["tracks"])

# FR-1 item 5 (agent parity for the UI "Hide broken links" toggle). Any
# other value is a 422 straight from FastAPI's Literal validation.
AvailableFilter = Literal["all", "true", "false"]


def keep_by_availability(available: AvailableFilter, file_exists: bool) -> bool:
    """True when a row passes the ?available filter (explicit three-state)."""
    if available == "all":
        return True
    if available == "true":
        return file_exists
    if available == "false":
        return not file_exists
    raise AssertionError(f"unhandled available filter: {available}")


def _has_rb_mapping(stable_id: str) -> bool:
    """Same criteria build_track_rows uses: a live vendor mapping AND a live
    djmdContent row (rb_vendor.bulk_rb_meta yields a row only for both)."""
    return stable_id in rb_vendor.bulk_rb_meta([stable_id])


def _rb_mapping_and_artwork(
    stable_id: str, file_path: str | None
) -> tuple[bool, bool | None]:
    """One bulk_rb_meta for both has_rb_mapping and listing's artwork facts."""
    meta_map = rb_vendor.bulk_rb_meta([stable_id])
    artwork_available, _status = _artwork_facts(meta_map.get(stable_id), file_path)
    return stable_id in meta_map, artwork_available


def _data_dir_from_state_db(state_db_path: Path) -> Path:
    return state_db_path.parent.parent


def _analysis_db_path(request: Request) -> Path:
    override: Path | None = getattr(request.app.state, "analysis_db_path", None)
    return Path(override) if override is not None else STATE_DB


def _stems_dir(request: Request) -> Path:
    configured = getattr(request.app.state, "stems_dir", DEFAULT_STEMS_DIR)
    return Path(configured)


def _lyrics_available_bulk(data_dir: Path, stable_ids: list[str]) -> dict[str, bool]:
    return {
        sid: lyrics_cache.cache_path(data_dir, sid).is_file()
        for sid in stable_ids
    }


def _auto_cues_available_bulk(
    analysis_db_path: Path, stable_ids: list[str]
) -> dict[str, bool]:
    if not stable_ids:
        return {}
    available = {sid: False for sid in stable_ids}
    if not analysis_db_path.exists():
        return available
    placeholders = ",".join("?" * len(stable_ids))
    sql = (
        f"SELECT DISTINCT stable_id FROM analysis WHERE stable_id IN ({placeholders})"
        " AND backend NOT LIKE 'own\\_%' ESCAPE '\\'"
    )
    conn = sqlite3.connect(f"file:{analysis_db_path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        try:
            for (sid,) in conn.execute(sql, stable_ids):
                available[str(sid)] = True
        except sqlite3.OperationalError as exc:
            if "no such table: analysis" not in str(exc):
                raise
    finally:
        conn.close()
    return available


def _stems_available_from_summary(stems: dict[str, Any]) -> bool:
    # Skip GET /stems only when summary is "none" (404). "invalid" still probes (422).
    return (stems or {}).get("status") != "none"


_STEM_INDEX_CACHE_LOCK = threading.Lock()
_STEM_INDEX_CACHE: dict[Path, tuple[float | None, stem_index.StemAssetIndex]] = {}


def _cached_stem_index(data_dir: Path) -> stem_index.StemAssetIndex:
    """Load the local R2 stem index cache, re-parsing only when its mtime
    changes -- a track-listing page's N rows cost one file read, not N."""
    path = stem_index.local_index_cache_path(data_dir)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        mtime = None
    with _STEM_INDEX_CACHE_LOCK:
        cached = _STEM_INDEX_CACHE.get(data_dir)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    index = stem_index.load_cached_index(data_dir)
    with _STEM_INDEX_CACHE_LOCK:
        _STEM_INDEX_CACHE[data_dir] = (mtime, index)
    return index


def _stems_available(stable_id: str, stems: dict[str, Any], request: Request) -> bool:
    """True from the local summary, OR (when on-demand hydration is wired on
    this app -- ``app_wiring._bind_stem_hydration``) from the cached R2 index,
    so a bundle absent locally but indexed still reports available and the
    frontend's stems probe actually issues the manifest GET that starts
    hydration (see apps/webui/server/routes/stems.py `_enqueue_hydration`)."""
    if _stems_available_from_summary(stems):
        return True
    data_dir = getattr(request.app.state, "stem_hydration_data_dir", None)
    if data_dir is None:
        return False
    return stable_id in _cached_stem_index(Path(data_dir))


def _optional_resource_flags(
    stable_id: str,
    *,
    request: Request,
    data_dir: Path,
    analysis_db_path: Path,
    stems: dict[str, Any],
) -> tuple[bool, bool, bool]:
    lyrics = _lyrics_available_bulk(data_dir, [stable_id])[stable_id]
    auto_cues = _auto_cues_available_bulk(analysis_db_path, [stable_id])[stable_id]
    return lyrics, auto_cues, _stems_available(stable_id, stems, request)


def _track_to_out(
    track: Track,
    has_rb_mapping: bool,
    *,
    lyrics_available: bool,
    auto_cues_available: bool,
    stems_available: bool,
    artwork_available: bool | None,
) -> TrackOut:
    prov_out = {
        k: ProvenanceOut(
            value=v.value,
            source=v.source,
            confidence=v.confidence,
            modified_at=v.modified_at,
            status=v.status,
            reason=v.reason,
        )
        for k, v in (track.provenance or {}).items()
    }
    return TrackOut(
        stable_id=track.stable_id,
        title=track.title,
        artist=track.artist,
        album=track.album,
        duration_ms=track.duration_ms,
        bpm=track.bpm,
        key=track.key,
        rating=track.rating,
        tags=list(track.tags or []),
        notes=track.notes,
        last_played_at=track.last_played_at,
        tempo_pref=track.tempo_pref,
        file_path=track.file_path,
        created_at=track.created_at,
        updated_at=track.updated_at,
        provenance=prov_out,
        has_rb_mapping=has_rb_mapping,
        lyrics_available=lyrics_available,
        auto_cues_available=auto_cues_available,
        stems_available=stems_available,
        artwork_available=artwork_available,
    )


@router.get("", response_model=TracksPage)
def list_tracks(
    request: Request,
    q: str | None = Query(None, description="Substring match on title/artist"),
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    key: str | None = None,
    rating_min: int | None = None,
    tag: str | None = None,
    available: AvailableFilter = Query(  # noqa: B008  # FastAPI DI
        "all",
        description=(
            "Filter rows on file_exists disk truth (FR-1 agent parity). "
            "Applied to the page AFTER cursor pagination, so a page may "
            "return fewer than `limit` rows while next_cursor still "
            "advances over the full track set."
        ),
    ),
    show_deleted: bool = Query(
        False,
        description="When true, include rows with tracks.deleted_at set. Default hides them.",
    ),
    cursor: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> TracksPage:
    flt = TrackFilter(
        q=q,
        bpm_min=bpm_min,
        bpm_max=bpm_max,
        key=key,
        rating_min=rating_min,
        tag=tag,
        cursor=cursor,
        limit=limit,
        show_deleted=show_deleted,
    )
    page = backend.list_tracks(flt)
    jobs_store = getattr(request.app.state, "jobs_store", None)
    rows = rb_vendor.build_track_rows(page.items, jobs_store=jobs_store)
    stable_ids = [t.stable_id for t in page.items]
    state_db_path = Path(request.app.state.state_db_path)
    data_dir = _data_dir_from_state_db(state_db_path)
    lyrics_by_sid = _lyrics_available_bulk(data_dir, stable_ids)
    auto_cues_by_sid = _auto_cues_available_bulk(_analysis_db_path(request), stable_ids)
    items: list[TrackListItemOut] = []
    for track, row in zip(page.items, rows, strict=False):
        if not keep_by_availability(available, row["file_exists"]):
            continue
        base = _track_to_out(
            track,
            has_rb_mapping=row["has_rb_mapping"],
            lyrics_available=lyrics_by_sid[track.stable_id],
            auto_cues_available=auto_cues_by_sid[track.stable_id],
            stems_available=_stems_available(track.stable_id, row["stems"], request),
            artwork_available=row["artwork_available"],
        ).model_dump()
        base["play_count"] = int(row.get("play_count") or 0)
        items.append(
            TrackListItemOut(
                **base,
                preview_b64=row["preview_b64"],
                preview_max=row["preview_max"],
                file_exists=row["file_exists"],
                is_remote=bool(row.get("is_remote")),
                has_remote_copy=bool(row["has_remote_copy"]),
                cloud_transfer=row["cloud_transfer"],
                quality=row["quality"],
                vocals=row["vocals"],
                stems=row["stems"],
                artwork_status=row["artwork_status"],
                energy=row["energy"],
                energy_source=row["energy_source"],
                energy_reason=row["energy_reason"],
                lyrics=row.get("lyrics"),
                is_remix=bool(row.get("is_remix")),
                is_radio_edit=bool(row.get("is_radio_edit")),
            )
        )
    return TracksPage(items=items, next_cursor=page.next_cursor)


# Declared BEFORE /{stable_id} so "quality-ladder" is not swallowed as an id.
@router.get("/quality-ladder", response_model=list[QualityRungOut])
def get_quality_ladder() -> list[QualityRungOut]:
    """The six venue rungs, so the UI legend is not a second copy of them."""
    return [QualityRungOut(**rung) for rung in audio_quality.ladder()]


class LyricsCachedIdsOut(BaseModel):
    """Stable ids with a valid on-disk lyrics-cache entry."""

    stable_ids: list[str] = Field(default_factory=list)


class TrackMembershipRefOut(BaseModel):
    playlist_id: str
    position: int


class TrackLifecycleOut(BaseModel):
    stable_id: str
    deleted_at: str | None
    memberships: list[TrackMembershipRefOut]


@router.get("/lyrics-cached-ids", response_model=LyricsCachedIdsOut)
def get_lyrics_cached_ids(request: Request) -> LyricsCachedIdsOut:
    """List cached lyric timelines so the UI can skip explicit cache-miss reads."""
    state_db_path = Path(request.app.state.state_db_path)
    done, _corrupt = valid_lyrics_ids(lyrics_cache.cache_dir(state_db_path.parent.parent))
    return LyricsCachedIdsOut(stable_ids=sorted(done))


@router.get(
    "/{stable_id}/lyrics",
    response_model=TrackLyricsOut,
    responses={
        404: {
            "model": LyricsUnavailableOut,
            "description": "No cached line-synced lyrics exist for this track.",
        }
    },
)
def get_track_lyrics(stable_id: str, request: Request) -> TrackLyricsOut:
    """Read the real cached line timeline without fetching or inventing lyrics."""
    state_db_path = Path(request.app.state.state_db_path)
    lyrics = lyrics_cache.load(lyrics_cache.cache_path(state_db_path.parent.parent, stable_id))
    if lyrics is None:
        raise HTTPException(status_code=404, detail=f"no cached lyrics for {stable_id!r}")
    if lyrics.stable_id != stable_id:
        raise ValueError(f"lyrics-cache identity mismatch for {stable_id!r}")
    if not lyrics.lines:
        raise ValueError(f"lyrics-cache contains no line-level lyrics for {stable_id!r}")
    return TrackLyricsOut(
        stable_id=lyrics.stable_id,
        source=lyrics.source,
        lines=[{"start_ms": line.start_ms, "text": line.text} for line in lyrics.lines],
    )


@router.get("/{stable_id}/playlists", response_model=list[TrackPlaylistOut])
def list_track_playlists(
    stable_id: str,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> list[TrackPlaylistOut]:
    """Live playlists that currently hold this track (LIBM-29).

    404 if the stable_id has no tracks row. 200 [] if the track exists
    but has no live memberships. Tombstoned memberships and deleted
    playlists are excluded.
    """
    backend.get_track(stable_id)
    return [
        TrackPlaylistOut(
            playlist_id=hit.playlist_id,
            name=hit.name,
            vendor=hit.vendor,
            positions=list(hit.positions),
        )
        for hit in backend.list_track_playlists(stable_id)
    ]


def _lifecycle_out(result: TrackLifecycleResult) -> TrackLifecycleOut:
    return TrackLifecycleOut(
        stable_id=result.stable_id,
        deleted_at=result.deleted_at,
        memberships=[
            TrackMembershipRefOut(
                playlist_id=membership.playlist_id,
                position=membership.position,
            )
            for membership in result.memberships
        ],
    )


def _run_track_lifecycle(
    request: Request,
    stable_id: str,
    action: str,
) -> TrackLifecycleResult:
    db_path = Path(request.app.state.state_db_path)
    if not db_path.is_file():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "state_db_missing",
                "message": (
                    f"state DB not found at {db_path}; track lifecycle writes "
                    "require an initialised state.db"
                ),
            },
        )
    conn = state_db.open_rw(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        with StateWriter(conn, actor="webui") as writer:
            if action == "remove":
                result = writer.remove_from_library(stable_id)
            elif action == "undelete":
                result = writer.undelete_track(stable_id)
            else:
                raise AssertionError(f"unknown track lifecycle action: {action}")
        conn.commit()
        return result
    except TrackNotFoundError as exc:
        conn.rollback()
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": str(exc)},
        ) from exc
    except TrackAlreadyRemovedError as exc:
        conn.rollback()
        raise HTTPException(
            status_code=409,
            detail={"error": "already_removed", "message": str(exc)},
        ) from exc
    except TrackNotRemovedError as exc:
        conn.rollback()
        raise HTTPException(
            status_code=409,
            detail={"error": "not_removed", "message": str(exc)},
        ) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@router.post("/{stable_id}:remove", response_model=TrackLifecycleOut)
def remove_track_from_library(
    stable_id: str,
    request: Request,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> TrackLifecycleOut:
    """Soft-delete a track from the library while keeping the audio file on disk.

    Uses ``POST :remove`` rather than ``DELETE /tracks/{stable_id}`` so
    ``LIBM-53`` can own file deletion later as a separate, harder action.
    """
    result = _run_track_lifecycle(request, stable_id, "remove")
    publish(
        "library.changed",
        {
            "kind": "tracks",
            "ids": [stable_id],
            "playlist_ids": [m.playlist_id for m in result.memberships],
        },
    )
    return _lifecycle_out(result)


@router.post("/{stable_id}:undelete", response_model=TrackLifecycleOut)
def undelete_track_from_library(
    stable_id: str,
    request: Request,
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> TrackLifecycleOut:
    """Restore a tombstoned track and the memberships this remove stamped."""
    result = _run_track_lifecycle(request, stable_id, "undelete")
    publish(
        "library.changed",
        {
            "kind": "tracks",
            "ids": [stable_id],
            "playlist_ids": [m.playlist_id for m in result.memberships],
        },
    )
    return _lifecycle_out(result)


@router.get("/{stable_id}", response_model=TrackOut)
def get_track(
    stable_id: str,
    request: Request,
    response: Response,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> TrackOut:
    # NotFoundError -> handle_not_found (errors.py).
    track = backend.get_track(stable_id)
    response.headers["ETag"] = compute_etag(track.stable_id, track.updated_at, track.selection_tag)
    state_db_path = Path(request.app.state.state_db_path)
    data_dir = _data_dir_from_state_db(state_db_path)
    stems = bulk_stem_summaries([stable_id], stems_dir=_stems_dir(request))[stable_id]
    lyrics, auto_cues, stems_avail = _optional_resource_flags(
        stable_id,
        request=request,
        data_dir=data_dir,
        analysis_db_path=_analysis_db_path(request),
        stems=stems,
    )
    has_rb_mapping, artwork_available = _rb_mapping_and_artwork(
        stable_id, track.file_path
    )
    return _track_to_out(
        track,
        has_rb_mapping=has_rb_mapping,
        lyrics_available=lyrics,
        auto_cues_available=auto_cues,
        stems_available=stems_avail,
        artwork_available=artwork_available,
    )


@router.post("/{stable_id}:reveal", status_code=204, operation_id="reveal_track")
def reveal_track(
    stable_id: str,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> Response:
    """Reveal a track's local file in the OS file manager."""
    track = backend.get_track(stable_id)
    try:
        reveal_track_path(track.file_path)
    except RevealPathError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return Response(status_code=204)


@router.patch("/{stable_id}", response_model=TrackOut)
def patch_track(
    stable_id: str,
    patch: TrackPatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
):
    if not if_match:
        return precondition_required(
            "PATCH /tracks/{stable_id} requires If-Match header"
        )
    patch_dict: dict = {}
    if patch.rating is not None or "rating" in patch.model_fields_set:
        patch_dict["rating"] = patch.rating
    if patch.notes is not None or "notes" in patch.model_fields_set:
        patch_dict["notes"] = patch.notes
    if patch.genre is not None or "genre" in patch.model_fields_set:
        patch_dict["genre"] = patch.genre
    if patch.comments is not None or "comments" in patch.model_fields_set:
        patch_dict["comments"] = patch.comments
    if patch.tags_add is not None:
        patch_dict["tags_add"] = patch.tags_add
    if patch.tags_remove is not None:
        patch_dict["tags_remove"] = patch.tags_remove
    if patch.tempo_pref is not None or "tempo_pref" in patch.model_fields_set:
        patch_dict["tempo_pref"] = (
            patch.tempo_pref.model_dump() if patch.tempo_pref is not None else None
        )
    try:
        updated = backend.update_track(
            stable_id, patch_dict, expected_etag=if_match, source="webui"
        )
    except ConflictError as exc:
        # update_track raises straight from the backend layer (current.to_dict()),
        # bypassing _track_to_out, so the 409 payload never picks up has_rb_mapping
        # on its own; the track page's "Take theirs" reads this field off it (#736
        # review). Same criteria as every other TrackOut response, computed here
        # since only the route layer has _has_rb_mapping.
        exc.current["has_rb_mapping"] = _has_rb_mapping(stable_id)
        raise
    response.headers["ETag"] = compute_etag(
        updated.stable_id, updated.updated_at, updated.selection_tag
    )
    publish("library.changed", {"kind": "tracks", "ids": [updated.stable_id]})
    state_db_path = Path(request.app.state.state_db_path)
    data_dir = _data_dir_from_state_db(state_db_path)
    stems = bulk_stem_summaries([stable_id], stems_dir=_stems_dir(request))[stable_id]
    lyrics, auto_cues, stems_avail = _optional_resource_flags(
        stable_id,
        request=request,
        data_dir=data_dir,
        analysis_db_path=_analysis_db_path(request),
        stems=stems,
    )
    has_rb_mapping, artwork_available = _rb_mapping_and_artwork(
        stable_id, updated.file_path
    )
    return _track_to_out(
        updated,
        has_rb_mapping=has_rb_mapping,
        lyrics_available=lyrics,
        auto_cues_available=auto_cues,
        stems_available=stems_avail,
        artwork_available=artwork_available,
    )
