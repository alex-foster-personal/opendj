"""Tracks endpoints (list / get / patch) -- CAT-05."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, Query, Response

from apps.shared import audio_quality
from apps.shared.events import publish

from .. import rb_vendor
from ..backend import StateBackend, Track, TrackFilter
from ..deps import get_read_state, get_write_state
from ..errors import precondition_required
from ..etag import compute_etag
from ..models import (
    ProvenanceOut,
    QualityRungOut,
    TrackListItemOut,
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


def _track_to_out(track: Track) -> TrackOut:
    prov_out = {
        k: ProvenanceOut(
            value=v.value,
            source=v.source,
            confidence=v.confidence,
            modified_at=v.modified_at,
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
    )


@router.get("", response_model=TracksPage)
def list_tracks(
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
    items: list[TrackListItemOut] = []
    for track, row in zip(page.items, rows):
        if not keep_by_availability(available, row["file_exists"]):
            continue
        base = _track_to_out(track).model_dump()
        base["play_count"] = int(row.get("play_count") or 0)
        items.append(
            TrackListItemOut(
                **base,
                preview_b64=row["preview_b64"],
                preview_max=row["preview_max"],
                file_exists=row["file_exists"],
                quality=row["quality"],
                vocals=row["vocals"],
                stems=row["stems"],
                has_rb_mapping=row["has_rb_mapping"],
            )
        )
    return TracksPage(items=items, next_cursor=page.next_cursor)


# Declared BEFORE /{stable_id} so "quality-ladder" is not swallowed as an id.
@router.get("/quality-ladder", response_model=list[QualityRungOut])
def get_quality_ladder() -> list[QualityRungOut]:
    """The six venue rungs, so the UI legend is not a second copy of them."""
    return [QualityRungOut(**rung) for rung in audio_quality.ladder()]


@router.get("/{stable_id}", response_model=TrackOut)
def get_track(
    stable_id: str,
    response: Response,
    backend: StateBackend = Depends(get_read_state),
) -> TrackOut:
    # NotFoundError -> handle_not_found (errors.py).
    track = backend.get_track(stable_id)
    response.headers["ETag"] = compute_etag(track.stable_id, track.updated_at)
    return _track_to_out(track)


@router.patch("/{stable_id}", response_model=TrackOut)
def patch_track(
    stable_id: str,
    patch: TrackPatch,
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
    updated = backend.update_track(
        stable_id, patch_dict, expected_etag=if_match, source="webui"
    )
    response.headers["ETag"] = compute_etag(updated.stable_id, updated.updated_at)
    publish("library.changed", {"kind": "tracks", "ids": [updated.stable_id]})
    return _track_to_out(updated)
