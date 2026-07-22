"""Atomic management endpoints for the custom ``Track.tags`` catalog."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from ..backend import BatchConflictError, StateBackend, TrackFilter, TrackUpdate
from ..deps import get_read_state, get_write_state
from ..etag import compute_etag

router = APIRouter(prefix="/mytags", tags=["mytags"])
_SCAN_PAGE_LIMIT = 1000


class MyTagSummary(BaseModel):
    name: str
    track_count: int


class MyTagListOut(BaseModel):
    tags: list[MyTagSummary]


class MyTagAssignIn(BaseModel):
    stable_ids: list[str] = Field(min_length=1)
    expected_etags: dict[str, str]
    add: list[str] = []
    remove: list[str] = []

    @model_validator(mode="after")
    def _has_changes(self) -> "MyTagAssignIn":
        if not self.add and not self.remove:
            raise ValueError("at least one of add/remove is required")
        return self


class MyTagAssignRowOut(BaseModel):
    stable_id: str
    tags: list[str]
    etag: str


class MyTagAssignOut(BaseModel):
    applied_count: int
    results: list[MyTagAssignRowOut]


class MyTagRenameIn(BaseModel):
    old_name: str = Field(min_length=1)
    new_name: str = Field(min_length=1)

    @model_validator(mode="after")
    def _names_differ(self) -> "MyTagRenameIn":
        if self.old_name == self.new_name:
            raise ValueError("old_name and new_name must differ")
        return self


class MyTagDeleteIn(BaseModel):
    name: str = Field(min_length=1)


class MyTagSweepOut(BaseModel):
    tracks_updated: int


def _all_tracks(backend: StateBackend, tag: str | None = None) -> list:
    tracks: list = []
    cursor: str | None = None
    while True:
        page = backend.list_tracks(TrackFilter(tag=tag, cursor=cursor, limit=_SCAN_PAGE_LIMIT))
        tracks.extend(page.items)
        if page.next_cursor is None:
            return tracks
        cursor = page.next_cursor


def _raise_conflict(exc: BatchConflictError) -> None:
    raise HTTPException(
        status_code=409,
        detail={"error": "conflict", "conflicts": exc.conflicts},
    ) from exc


@router.get("", response_model=MyTagListOut)
def list_mytags(backend: StateBackend = Depends(get_read_state)) -> MyTagListOut:
    counts: dict[str, int] = {}
    for track in _all_tracks(backend):
        for tag in track.tags or []:
            counts[tag] = counts.get(tag, 0) + 1
    return MyTagListOut(
        tags=[MyTagSummary(name=name, track_count=count) for name, count in sorted(counts.items())],
    )


@router.post("/assign", response_model=MyTagAssignOut)
def assign_mytags(body: MyTagAssignIn, backend: StateBackend = Depends(get_write_state)) -> MyTagAssignOut:
    missing = [stable_id for stable_id in body.stable_ids if stable_id not in body.expected_etags]
    if missing:
        raise HTTPException(status_code=422, detail={"error": "missing_expected_etag", "stable_ids": missing})
    patch: dict[str, list[str]] = {}
    if body.add:
        patch["tags_add"] = body.add
    if body.remove:
        patch["tags_remove"] = body.remove
    try:
        tracks = backend.update_tracks(
            [TrackUpdate(stable_id, patch, body.expected_etags[stable_id]) for stable_id in body.stable_ids],
            source="webui",
        )
    except BatchConflictError as exc:
        _raise_conflict(exc)
    return MyTagAssignOut(
        applied_count=len(tracks),
        results=[MyTagAssignRowOut(stable_id=track.stable_id, tags=track.tags, etag=compute_etag(track.stable_id, track.updated_at)) for track in tracks],
    )


def _sweep_tag(backend: StateBackend, old_name: str, new_name: str | None) -> int:
    try:
        return backend.update_tag_members(old_name, new_name, source="webui")
    except BatchConflictError as exc:
        _raise_conflict(exc)


@router.post("/rename", response_model=MyTagSweepOut)
def rename_mytag(body: MyTagRenameIn, backend: StateBackend = Depends(get_write_state)) -> MyTagSweepOut:
    return MyTagSweepOut(tracks_updated=_sweep_tag(backend, body.old_name, body.new_name))


@router.post("/delete", response_model=MyTagSweepOut)
def delete_mytag(body: MyTagDeleteIn, backend: StateBackend = Depends(get_write_state)) -> MyTagSweepOut:
    return MyTagSweepOut(tracks_updated=_sweep_tag(backend, body.name, None))


__all__ = ["router"]
