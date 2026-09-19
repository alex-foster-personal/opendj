"""Bulk edit across selected tracks (bulk-edit, issue #178).

One endpoint, PATCH-shaped semantics but multi-row: sets the same
rating/notes/genre/comments/tags_add/tags_remove across every stable_id in the request.
Two-phase like find-replace (see find_replace.py docstring): every row's
expected_etag is checked against the live etag with zero writes first: if
ANY row has drifted the whole batch is rejected (409, nothing applied).
Only once every row clears the pre-check are the writes issued, one per
row through the existing StateBackend.update_track chokepoint.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from apps.shared.events import publish

from ..backend import BatchConflictError, NotFoundError, StateBackend, TrackUpdate
from ..deps import get_write_state
from ..etag import compute_etag

router = APIRouter(prefix="/bulk-edit", tags=["bulk-edit"])

# Same cap as find_replace.py's _MAX_STABLE_IDS: an unbounded batch has no
# input-layer rejection, so it is only ever stopped deep inside the backend
# (a 404 on the first unknown id, or a full per-row etag scan), never
# cleanly and never before doing that much work.
_MAX_STABLE_IDS = 100


class BulkEditIn(BaseModel):
    stable_ids: list[str] = Field(min_length=1, max_length=_MAX_STABLE_IDS)
    expected_etags: dict[str, str]
    rating: int | None = None
    notes: str | None = None
    genre: str | None = None
    comments: str | None = None
    tags_add: list[str] | None = None
    tags_remove: list[str] | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "BulkEditIn":
        if (
            self.rating is None
            and self.notes is None
            and self.genre is None
            and self.comments is None
            and self.tags_add is None
            and self.tags_remove is None
        ):
            raise ValueError(
                "at least one of rating/notes/genre/comments/tags_add/tags_remove is required"
            )
        if self.rating is not None and not (0 <= self.rating <= 5):
            raise ValueError("rating must be between 0 and 5")
        return self


class BulkEditRowOut(BaseModel):
    stable_id: str
    etag: str


class BulkEditOut(BaseModel):
    applied_count: int
    results: list[BulkEditRowOut]


@router.patch("", response_model=BulkEditOut)
def bulk_edit(
    body: BulkEditIn,
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> BulkEditOut:
    missing_etags = [sid for sid in body.stable_ids if sid not in body.expected_etags]
    if missing_etags:
        raise HTTPException(
            status_code=422,
            detail={"error": "missing_expected_etag", "stable_ids": missing_etags},
        )

    patch_dict: dict = {}
    if body.rating is not None:
        patch_dict["rating"] = body.rating
    if body.notes is not None:
        patch_dict["notes"] = body.notes
    if body.genre is not None:
        patch_dict["genre"] = body.genre
    if body.comments is not None:
        patch_dict["comments"] = body.comments
    if body.tags_add is not None:
        patch_dict["tags_add"] = body.tags_add
    if body.tags_remove is not None:
        patch_dict["tags_remove"] = body.tags_remove

    updates = [
        TrackUpdate(stable_id, patch_dict, body.expected_etags[stable_id])
        for stable_id in body.stable_ids
    ]
    try:
        tracks = backend.update_tracks(updates, source="webui")
    except BatchConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "conflict", "conflicts": exc.conflicts},
        ) from exc
    except NotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": str(exc)},
        ) from exc
    results = [
        BulkEditRowOut(
            stable_id=track.stable_id,
            etag=compute_etag(track.stable_id, track.updated_at, track.selection_tag),
        )
        for track in tracks
    ]
    _publish_tracks_changed(results)

    return BulkEditOut(applied_count=len(results), results=results)


def _publish_tracks_changed(results: list[BulkEditRowOut]) -> None:
    publish("library.changed", {"kind": "tracks", "ids": [r.stable_id for r in results]})


__all__ = ["router"]
