"""Tracks endpoints (list / get / patch) -- CAT-05."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response

from apps.lyrics import cache as lyrics_cache
from apps.shared import audio_quality
from apps.shared.events import publish
from apps.shared.paths import STATE_DB

from .. import rb_vendor
from ..backend import ConflictError, StateBackend, Track, TrackFilter
from ..deps import get_read_state, get_write_state
from ..errors import precondition_required
from ..etag import compute_etag
from ..stem_artifacts import DEFAULT_STEMS_DIR, bulk_stem_summaries
from ..models import (
    LyricsUnavailableOut,
    ProvenanceOut,
    QualityRungOut,
    TrackListItemOut,
    TrackLyricsOut,
    TrackOut,
    TrackPatch,
    TracksPage,
)

router = APIRouter(prefix="/tracks", tags=["tracks"])

# FR-1 item 5 (agent parity for the UI "Hide broken links" toggle). Any
# other value is a 422 straight from FastAPI's Literal validation.
AvailableFilter = Literal["all", "true", "false"]


def keep_by_availability(available: AvailableFilter, file_exists: bool) -> bool:
    """True when a row passes the ?available filter (explicit three-state)."""
    if available == "all":
        return True
    elif available == "true":
        return file_exists
    elif available == "false":
        return not file_exists
    raise AssertionError(f"unhandled available filter: {available}")


def _has_rb_mapping(stable_id: str) -> bool:
    """Same criteria build_track_rows uses: a live vendor mapping AND a live
    djmdContent row (rb_vendor.bulk_rb_meta yields a row only for both)."""
    return stable_id in rb_vendor.bulk_rb_meta([stable_id])


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


def _optional_resource_flags(
    stable_id: str,
    *,
    data_dir: Path,
    analysis_db_path: Path,
    stems: dict[str, Any],
) -> tuple[bool, bool, bool]:
    lyrics = _lyrics_available_bulk(data_dir, [stable_id])[stable_id]
    auto_cues = _auto_cues_available_bulk(analysis_db_path, [stable_id])[stable_id]
    return lyrics, auto_cues, _stems_available_from_summary(stems)


def _artwork_available_for_track(track: Track) -> bool | None:
    """Same predicate listing rows use via build_track_rows."""
    rows = rb_vendor.build_track_rows([track])
    return rows[0]["artwork_available"]


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
    q: Optional[str] = Query(None, description="Substring match on title/artist"),
    bpm_min: Optional[float] = None,
    bpm_max: Optional[float] = None,
    key: Optional[str] = None,
    rating_min: Optional[int] = None,
    tag: Optional[str] = None,
    available: AvailableFilter = Query(
        "all",
        description=(
            "Filter rows on file_exists disk truth (FR-1 agent parity). "
            "Applied to the page AFTER cursor pagination, so a page may "
            "return fewer than `limit` rows while next_cursor still "
            "advances over the full track set."
        ),
    ),
    cursor: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    backend: StateBackend = Depends(get_read_state),
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
    )
    page = backend.list_tracks(flt)
    rows = rb_vendor.build_track_rows(page.items)
    stable_ids = [t.stable_id for t in page.items]
    state_db_path = Path(request.app.state.state_db_path)
    data_dir = _data_dir_from_state_db(state_db_path)
    lyrics_by_sid = _lyrics_available_bulk(data_dir, stable_ids)
    auto_cues_by_sid = _auto_cues_available_bulk(_analysis_db_path(request), stable_ids)
    items: list[TrackListItemOut] = []
    for track, row in zip(page.items, rows):
        if not keep_by_availability(available, row["file_exists"]):
            continue
        base = _track_to_out(
            track,
            has_rb_mapping=row["has_rb_mapping"],
            lyrics_available=lyrics_by_sid[track.stable_id],
            auto_cues_available=auto_cues_by_sid[track.stable_id],
            stems_available=_stems_available_from_summary(row["stems"]),
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
                quality=row["quality"],
                vocals=row["vocals"],
                stems=row["stems"],
                artwork_status=row["artwork_status"],
                energy=row["energy"],
                energy_source=row["energy_source"],
                energy_reason=row["energy_reason"],
            )
        )
    return TracksPage(items=items, next_cursor=page.next_cursor)


# Declared BEFORE /{stable_id} so "quality-ladder" is not swallowed as an id.
@router.get("/quality-ladder", response_model=list[QualityRungOut])
def get_quality_ladder() -> list[QualityRungOut]:
    """The six venue rungs, so the UI legend is not a second copy of them."""
    return [QualityRungOut(**rung) for rung in audio_quality.ladder()]


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


@router.get("/{stable_id}", response_model=TrackOut)
def get_track(
    stable_id: str,
    request: Request,
    response: Response,
    backend: StateBackend = Depends(get_read_state),
) -> TrackOut:
    # NotFoundError -> handle_not_found (errors.py).
    track = backend.get_track(stable_id)
    response.headers["ETag"] = compute_etag(track.stable_id, track.updated_at, track.selection_tag)
    state_db_path = Path(request.app.state.state_db_path)
    data_dir = _data_dir_from_state_db(state_db_path)
    stems = bulk_stem_summaries([stable_id], stems_dir=_stems_dir(request))[stable_id]
    lyrics, auto_cues, stems_avail = _optional_resource_flags(
        stable_id,
        data_dir=data_dir,
        analysis_db_path=_analysis_db_path(request),
        stems=stems,
    )
    return _track_to_out(
        track,
        has_rb_mapping=_has_rb_mapping(stable_id),
        lyrics_available=lyrics,
        auto_cues_available=auto_cues,
        stems_available=stems_avail,
        artwork_available=_artwork_available_for_track(track),
    )


@router.patch("/{stable_id}", response_model=TrackOut)
def patch_track(
    stable_id: str,
    patch: TrackPatch,
    request: Request,
    response: Response,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    backend: StateBackend = Depends(get_write_state),
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
    if patch.tags_add is not None:
        patch_dict["tags_add"] = patch.tags_add
    if patch.tags_remove is not None:
        patch_dict["tags_remove"] = patch.tags_remove
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
        data_dir=data_dir,
        analysis_db_path=_analysis_db_path(request),
        stems=stems,
    )
    return _track_to_out(
        updated,
        has_rb_mapping=_has_rb_mapping(stable_id),
        lyrics_available=lyrics,
        auto_cues_available=auto_cues,
        stems_available=stems_avail,
        artwork_available=_artwork_available_for_track(updated),
    )
