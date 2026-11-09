"""Related Tracks route (LIBUX-10046, rekordbox Related Tracks parity).

GET /api/v1/tracks/{stable_id}/related scores every collection track except
the seed: one point per enabled rule it matches (rules and defaults in
``related_tracks.py``). Each criterion query param overrides the stored
``library_related_criteria`` ui-pref; an omitted one takes the stored value.
Agents change the stored criteria with PUT /api/v1/ui-prefs
``{"library_related_criteria": {...}}``, or read with ``opendj track related``.

Lives in its own module (not ``search.py``): each routes module mounts one
router, and search's router has prefix ``/search``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from .. import rb_vendor
from ..backend import MAX_LIMIT, StateBackend, Track, TrackFilter
from ..deps import get_library_data_dir, get_library_state_db, get_read_state
from ..library_related_prefs import LibraryRelatedCriteriaOut
from ..models import TrackRowOut
from ..rb_vendor_pkg.track_colors import bulk_rb_color_ids
from ..related_tracks import (
    BPM_PERCENT_MAX,
    BPM_PERCENT_MIN,
    RelatedCriteria,
    candidate_from_track,
    rank_related,
)
from .ui_prefs import read_library_related_criteria

router = APIRouter(prefix="/tracks", tags=["tracks"])
MOUNT_UNDER_API_PREFIX = True

DEFAULT_LIMIT = 50
RULE_LIMIT = 200

RuleName = Literal["bpm", "key", "genre", "color", "rating", "mytag", "artist"]


class RelatedHit(TrackRowOut):
    score: int
    matched: list[RuleName]


class RelatedResults(BaseModel):
    seed_stable_id: str
    criteria: LibraryRelatedCriteriaOut
    items: list[RelatedHit]
    total: int


def _all_tracks(backend: StateBackend) -> list[Track]:
    tracks: list[Track] = []
    cursor: str | None = None
    while True:
        page = backend.list_tracks(TrackFilter(cursor=cursor, limit=MAX_LIMIT))
        tracks.extend(page.items)
        if page.next_cursor is None:
            return tracks
        cursor = page.next_cursor


@router.get("/{stable_id}/related", response_model=RelatedResults)
def related_tracks(  # noqa: PLR0913, PLR0917  # FastAPI query params are the criteria contract
    stable_id: str,
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=RULE_LIMIT),
    bpm_percent: int | None = Query(None, ge=BPM_PERCENT_MIN, le=BPM_PERCENT_MAX),
    key: bool | None = None,
    genre: bool | None = None,
    color: bool | None = None,
    rating: bool | None = None,
    mytag: bool | None = None,
    artist: bool | None = None,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
    data_dir: Path = Depends(get_library_data_dir),  # noqa: B008  # FastAPI DI
    state_db: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> RelatedResults:
    seed_track = backend.get_track(stable_id)
    overrides = {
        "bpm_percent": bpm_percent,
        "key": key,
        "genre": genre,
        "color": color,
        "rating": rating,
        "mytag": mytag,
        "artist": artist,
    }
    stored = read_library_related_criteria(request)
    criteria = RelatedCriteria.from_dict({**stored, **{k: v for k, v in overrides.items() if v is not None}})

    tracks = _all_tracks(backend)
    colors = bulk_rb_color_ids([t.stable_id for t in tracks], state_db_path=state_db) if criteria.color else {}
    seed = candidate_from_track(seed_track, colors.get(seed_track.stable_id))
    ranked = rank_related(seed, (candidate_from_track(t, colors.get(t.stable_id)) for t in tracks), criteria)

    page = ranked[:limit]
    by_id = {t.stable_id: t for t in tracks}
    rows = rb_vendor.build_track_rows(
        [by_id[cand.stable_id] for cand, _ in page], data_dir=data_dir, state_db_path=state_db
    )
    matched_by_id = {cand.stable_id: matched for cand, matched in page}
    items = [
        RelatedHit(**row, score=len(matched_by_id[row["stable_id"]]), matched=list(matched_by_id[row["stable_id"]]))
        for row in rows
        if row["stable_id"] in matched_by_id
    ]
    return RelatedResults(
        seed_stable_id=stable_id,
        criteria=LibraryRelatedCriteriaOut(**criteria.to_dict()),
        items=items,
        total=len(ranked),
    )


__all__ = ["RelatedHit", "RelatedResults", "router"]
