"""Find and replace across a text track field (find-and-replace, issue #180).

Two endpoints:

  * POST /find-replace/preview - deterministic dry run. Never writes.
    Recomputes the replacement straight from each track's CURRENT live
    value, so repeated preview calls are byte-stable for the same
    underlying data (no client-supplied "current value" is trusted).
  * POST /find-replace/apply - two-phase apply: first every requested row's
    expected_etag is checked against the live etag with ZERO writes; if
    any row has drifted, the whole batch is rejected (409, nothing
    applied) rather than partially landing. Only once every row clears
    the pre-check does it write each changed row through the existing
    StateBackend.update_track chokepoint (StateWriter underneath, so the
    edit survives a daemon restart same as rating/notes/tags).

v1 scope: the only find-and-replace field is ``notes`` (the sole free-text
field already wired end-to-end through the state layer for webui edits -
title/artist/album are identity facts on ``tracks``, not the provenance-
wrapped write path this feature builds on. See RECON-FEATURES.md).
"""
from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..backend import BatchConflictError, NotFoundError, StateBackend, TrackUpdate
from ..deps import get_read_state, get_write_state
from ..etag import compute_etag

router = APIRouter(prefix="/find-replace", tags=["find-replace"])

FindReplaceField = Literal["notes"]
FindReplaceMode = Literal["literal", "regex"]
_MAX_REGEX_PATTERN_CHARS = 256
_MAX_REGEX_INPUT_CHARS = 10_000
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[*+][^()]*\)[*+{]")
_BACKREFERENCE = re.compile(r"\\[1-9]")


class FindReplaceScope(BaseModel):
    field: FindReplaceField = "notes"
    stable_ids: list[str] = Field(min_length=1)
    find: str = Field(min_length=1)
    replace: str = ""
    mode: FindReplaceMode = "literal"
    case_sensitive: bool = False


class FindReplacePreviewIn(FindReplaceScope):
    pass


class FindReplaceRowOut(BaseModel):
    stable_id: str
    current_value: str | None
    new_value: str | None
    would_change: bool
    etag: str


class FindReplacePreviewOut(BaseModel):
    results: list[FindReplaceRowOut]
    match_count: int


class FindReplaceApplyIn(FindReplaceScope):
    expected_etags: dict[str, str]


class FindReplaceApplyRowOut(BaseModel):
    stable_id: str
    new_value: str | None
    etag: str


class FindReplaceApplyOut(BaseModel):
    applied_count: int
    skipped_noop: list[str]
    results: list[FindReplaceApplyRowOut]


def _compile(find: str, mode: FindReplaceMode, case_sensitive: bool) -> re.Pattern[str]:
    if mode == "regex" and (
        len(find) > _MAX_REGEX_PATTERN_CHARS
        or _NESTED_QUANTIFIER.search(find) is not None
        or _BACKREFERENCE.search(find) is not None
    ):
        raise HTTPException(
            status_code=422,
            detail={"error": "unsafe_regex", "message": "regex exceeds the safe complexity policy"},
        )
    pattern = find if mode == "regex" else re.escape(find)
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_regex", "message": str(exc)},
        ) from exc


def _new_value(current: str | None, pattern: re.Pattern[str], replace: str) -> str | None:
    if current is None:
        return None
    if len(current) > _MAX_REGEX_INPUT_CHARS:
        raise HTTPException(
            status_code=422,
            detail={"error": "input_too_large", "message": "notes value exceeds regex safety limit"},
        )
    return pattern.sub(replace, current)


def _get_field(backend: StateBackend, stable_id: str, field: FindReplaceField) -> tuple[object, str, str]:
    """Returns (track, current field value, etag). 404s propagate."""
    try:
        track = backend.get_track(stable_id)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": str(exc), "stable_id": stable_id},
        ) from exc
    etag = compute_etag(track.stable_id, track.updated_at)
    return track, getattr(track, field), etag


@router.post("/preview", response_model=FindReplacePreviewOut)
def preview(
    body: FindReplacePreviewIn,
    backend: StateBackend = Depends(get_read_state),
) -> FindReplacePreviewOut:
    pattern = _compile(body.find, body.mode, body.case_sensitive)
    results: list[FindReplaceRowOut] = []
    match_count = 0
    for stable_id in body.stable_ids:
        _track, current, etag = _get_field(backend, stable_id, body.field)
        new_value = _new_value(current, pattern, body.replace)
        would_change = new_value != current
        if would_change:
            match_count += 1
        results.append(
            FindReplaceRowOut(
                stable_id=stable_id,
                current_value=current,
                new_value=new_value,
                would_change=would_change,
                etag=etag,
            )
        )
    return FindReplacePreviewOut(results=results, match_count=match_count)


@router.post("/apply", response_model=FindReplaceApplyOut)
def apply(
    body: FindReplaceApplyIn,
    backend: StateBackend = Depends(get_write_state),
) -> FindReplaceApplyOut:
    missing_etags = [sid for sid in body.stable_ids if sid not in body.expected_etags]
    if missing_etags:
        raise HTTPException(
            status_code=422,
            detail={"error": "missing_expected_etag", "stable_ids": missing_etags},
        )

    pattern = _compile(body.find, body.mode, body.case_sensitive)

    to_apply: list[tuple[str, str]] = []  # (stable_id, new_value)
    skipped_noop: list[str] = []
    for stable_id in body.stable_ids:
        _track, current, live_etag = _get_field(backend, stable_id, body.field)
        new_value = _new_value(current, pattern, body.replace)
        if new_value == current:
            skipped_noop.append(stable_id)
            continue
        to_apply.append((stable_id, new_value))

    replacement_by_id = dict(to_apply)
    updates = [
        TrackUpdate(
            stable_id,
            {body.field: replacement_by_id[stable_id]} if stable_id in replacement_by_id else {},
            body.expected_etags[stable_id],
        )
        for stable_id in body.stable_ids
    ]
    try:
        tracks = backend.update_tracks(updates, source="webui")
    except BatchConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "conflict", "conflicts": exc.conflicts},
        ) from exc
    results = [
        FindReplaceApplyRowOut(
            stable_id=track.stable_id,
            new_value=track.notes,
            etag=compute_etag(track.stable_id, track.updated_at),
        )
        for track in tracks
        if track.stable_id in replacement_by_id
    ]

    return FindReplaceApplyOut(
        applied_count=len(results), skipped_noop=skipped_noop, results=results
    )


__all__ = ["router"]
