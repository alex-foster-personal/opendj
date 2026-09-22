"""Autolists router -- lazy genre/rating/BPM bucket groups (issue #2066).

Read-only evaluation over Phase 5 tracks + track_fields. Does not persist
bucket definitions as smartlist rows or call the materialiser.
"""
from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from apps.smartlists.autolist_groups import (
    GROUP_IDS,
    AutolistSelectionError,
    buckets_with_counts,
    compact_index_rows,
    selection_to_rule,
)
from apps.smartlists.evaluator import EvaluatorError, evaluate

from .. import rb_vendor
from ..backend import StateBackend
from ..deps import get_read_state
from ..models import TrackRowOut
from .smartlists import get_smartlists_conn

router = APIRouter(prefix="/autolists", tags=["autolists"])

_DEFAULT_LIMIT = 500
_MAX_LIMIT = 500


class AutolistGroupOut(BaseModel):
    id: str
    title: str


class AutolistBucketOut(BaseModel):
    id: str
    label: str
    count: int | None = None


class AutolistIndexItem(BaseModel):
    stable_id: str
    genre: str | None = None
    rating: float | None = None
    bpm: float | None = None


class AutolistIndexOut(BaseModel):
    items: list[AutolistIndexItem]


class AutolistSelectionIn(BaseModel):
    genre: list[str] = Field(default_factory=list)
    rating: list[str] = Field(default_factory=list)
    bpm: list[str] = Field(default_factory=list)


class AutolistQueryIn(BaseModel):
    selection: AutolistSelectionIn
    offset: int = Field(0, ge=0)
    limit: int = Field(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT)


class AutolistQueryOut(BaseModel):
    items: list[str]
    tracks: list[TrackRowOut]
    total: int
    offset: int
    limit: int


_GROUP_TITLES: dict[str, str] = {
    "genre": "Genre",
    "rating": "Rating",
    "bpm": "BPM",
}


def _selection_dict(sel: AutolistSelectionIn) -> dict[str, list[str]]:
    return {"genre": sel.genre, "rating": sel.rating, "bpm": sel.bpm}


def _eval_or_503(rule: dict[str, Any], conn: sqlite3.Connection) -> list[str]:
    try:
        return evaluate(rule, conn)
    except EvaluatorError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "SMARTLIST_EVAL_UNAVAILABLE",
            "message": str(exc),
        }) from exc


@router.get("/groups", response_model=list[AutolistGroupOut])
def list_groups() -> list[AutolistGroupOut]:
    return [AutolistGroupOut(id=g, title=_GROUP_TITLES[g]) for g in GROUP_IDS]


@router.get("/buckets", response_model=list[AutolistBucketOut])
def list_buckets(
    conn: Annotated[sqlite3.Connection, Depends(get_smartlists_conn)],
    group: Literal["genre", "rating", "bpm"] = Query(...),
) -> list[AutolistBucketOut]:
    try:
        buckets = buckets_with_counts(conn, group)
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "SMARTLIST_EVAL_UNAVAILABLE",
            "message": str(exc),
        }) from exc
    return [AutolistBucketOut(id=b.id, label=b.label, count=b.count) for b in buckets]


@router.get("/index", response_model=AutolistIndexOut)
def get_index(
    conn: Annotated[sqlite3.Connection, Depends(get_smartlists_conn)],
) -> AutolistIndexOut:
    try:
        rows = compact_index_rows(conn)
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "SMARTLIST_EVAL_UNAVAILABLE",
            "message": str(exc),
        }) from exc
    return AutolistIndexOut(items=[AutolistIndexItem(**r) for r in rows])


@router.post("/query", response_model=AutolistQueryOut)
def query_autolists(
    body: AutolistQueryIn,
    conn: Annotated[sqlite3.Connection, Depends(get_smartlists_conn)],
    backend: Annotated[StateBackend, Depends(get_read_state)],
) -> AutolistQueryOut:
    sel = _selection_dict(body.selection)
    try:
        rule = selection_to_rule(sel)
    except AutolistSelectionError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "AUTOLIST_SELECTION_INVALID",
                "field": exc.field,
                "message": str(exc),
            },
        ) from exc
    if rule is None:
        return AutolistQueryOut(
            items=[], tracks=[], total=0, offset=body.offset, limit=body.limit,
        )
    stable_ids = _eval_or_503(rule, conn)
    total = len(stable_ids)
    page_ids = stable_ids[body.offset : body.offset + body.limit]
    tracks_map = backend.get_tracks_bulk(page_ids)
    missing = [sid for sid in page_ids if sid not in tracks_map]
    if missing:
        raise HTTPException(status_code=500, detail={
            "code": "SMARTLIST_MEMBER_MISSING",
            "message": (
                f"autolist query evaluated to {len(missing)} "
                f"stable_ids with no track row (first: {missing[:5]})"
            ),
        })
    rows = rb_vendor.build_track_rows([tracks_map[sid] for sid in page_ids])
    return AutolistQueryOut(
        items=stable_ids,
        tracks=[TrackRowOut(**r) for r in rows],
        total=total,
        offset=body.offset,
        limit=body.limit,
    )


__all__ = ["router"]
