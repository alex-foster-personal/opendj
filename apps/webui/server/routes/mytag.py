"""Atomic management endpoints for the custom ``Track.tags`` catalog."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from apps.shared.events import publish

from ..backend import (
    BatchConflictError,
    MyTagMergeConfirmationRequiredError,
    MyTagScopeConflictError,
    StateBackend,
    TrackFilter,
    TrackUpdate,
    compute_mytag_catalog_revision,
)
from ..deps import get_read_state, get_write_state
from ..etag import compute_etag

router = APIRouter(prefix="/mytags", tags=["mytags"])
_SCAN_PAGE_LIMIT = 1000
# Same cap as find_replace.py's _MAX_STABLE_IDS: an unbounded batch has no
# input-layer rejection, so it is only ever stopped deep inside the backend.
_MAX_STABLE_IDS = 100


class MyTagSummary(BaseModel):
    name: str
    track_count: int


class MyTagListOut(BaseModel):
    tags: list[MyTagSummary]
    catalog_revision: str


class MyTagAssignIn(BaseModel):
    stable_ids: list[str] = Field(min_length=1, max_length=_MAX_STABLE_IDS)
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


class MyTagSweepPreconditionIn(BaseModel):
    expected_catalog_revision: str = Field(min_length=1)
    expected_track_count: int = Field(ge=0)


class MyTagRenameIn(MyTagSweepPreconditionIn):
    old_name: str = Field(min_length=1)
    new_name: str = Field(min_length=1)
    confirm_merge: bool = False

    @model_validator(mode="after")
    def _names_differ(self) -> "MyTagRenameIn":
        if self.old_name == self.new_name:
            raise ValueError("old_name and new_name must differ")
        return self


class MyTagDeleteIn(MyTagSweepPreconditionIn):
    name: str = Field(min_length=1)


class MyTagSweepOut(BaseModel):
    tracks_updated: int


class MyTagScopeConflictDetail(BaseModel):
    error: Literal["stale_mytag_scope"]
    catalog_revision: str
    affected_track_count: int


class MyTagMergeConflictDetail(BaseModel):
    error: Literal["mytag_merge_confirmation_required"]
    destination_name: str


class MyTagScopeConflictOut(BaseModel):
    detail: MyTagScopeConflictDetail


class MyTagRenameConflictOut(BaseModel):
    detail: MyTagScopeConflictDetail | MyTagMergeConflictDetail


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
def list_mytags(backend: StateBackend = Depends(get_read_state)) -> MyTagListOut:  # noqa: B008  # FastAPI DI
    counts: dict[str, int] = {}
    tracks = _all_tracks(backend)
    for track in tracks:
        for tag in track.tags or []:
            counts[tag] = counts.get(tag, 0) + 1
    return MyTagListOut(
        tags=[MyTagSummary(name=name, track_count=count) for name, count in sorted(counts.items())],
        catalog_revision=compute_mytag_catalog_revision(tracks),
    )


@router.post("/assign", response_model=MyTagAssignOut)
def assign_mytags(body: MyTagAssignIn, backend: StateBackend = Depends(get_write_state)) -> MyTagAssignOut:  # noqa: B008  # FastAPI DI
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
    publish("library.changed", {"kind": "mytags", "ids": sorted({*body.add, *body.remove})})
    return MyTagAssignOut(
        applied_count=len(tracks),
        results=[MyTagAssignRowOut(stable_id=track.stable_id, tags=track.tags, etag=compute_etag(track.stable_id, track.updated_at, track.selection_tag)) for track in tracks],
    )


def _sweep_tag(
    backend: StateBackend, old_name: str, new_name: str | None, *,
    expected_catalog_revision: str, expected_track_count: int,
    confirm_merge: bool = False,
) -> int:
    try:
        return backend.update_tag_members(
            old_name, new_name,
            expected_catalog_revision=expected_catalog_revision,
            expected_track_count=expected_track_count,
            confirm_merge=confirm_merge,
            source="webui",
        )
    except BatchConflictError as exc:
        _raise_conflict(exc)
    except MyTagScopeConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale_mytag_scope",
                "catalog_revision": exc.catalog_revision,
                "affected_track_count": exc.affected_track_count,
            },
        ) from exc
    except MyTagMergeConfirmationRequiredError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "mytag_merge_confirmation_required",
                "destination_name": exc.destination_name,
            },
        ) from exc


@router.post(
    "/rename",
    response_model=MyTagSweepOut,
    responses={
        409: {
            "model": MyTagRenameConflictOut,
            "description": "The acknowledged scope is stale or merge consent is required.",
        },
    },
)
def rename_mytag(body: MyTagRenameIn, backend: StateBackend = Depends(get_write_state)) -> MyTagSweepOut:  # noqa: B008  # FastAPI DI
    tracks_updated = _sweep_tag(
        backend, body.old_name, body.new_name,
        expected_catalog_revision=body.expected_catalog_revision,
        expected_track_count=body.expected_track_count,
        confirm_merge=body.confirm_merge,
    )
    publish("library.changed", {"kind": "mytags", "ids": [body.old_name, body.new_name]})
    return MyTagSweepOut(tracks_updated=tracks_updated)


@router.post(
    "/delete",
    response_model=MyTagSweepOut,
    responses={
        409: {
            "model": MyTagScopeConflictOut,
            "description": "The acknowledged catalog scope is stale.",
        },
    },
)
def delete_mytag(body: MyTagDeleteIn, backend: StateBackend = Depends(get_write_state)) -> MyTagSweepOut:  # noqa: B008  # FastAPI DI
    tracks_updated = _sweep_tag(
        backend, body.name, None,
        expected_catalog_revision=body.expected_catalog_revision,
        expected_track_count=body.expected_track_count,
    )
    publish("library.changed", {"kind": "mytags", "ids": [body.name]})
    return MyTagSweepOut(tracks_updated=tracks_updated)


__all__ = ["router"]
