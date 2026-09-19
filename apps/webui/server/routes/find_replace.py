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

v1 scope: find-and-replace fields are ``notes``, ``genre``, and ``comments``
(the free-text fields wired end-to-end through the state layer for webui
edits - title/artist/album are identity facts on ``tracks``, not the
provenance-wrapped write path this feature builds on. See RECON-FEATURES.md).
"""

from __future__ import annotations

import time
from typing import Literal

import regex
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from apps.shared.events import publish

from ..backend import BatchConflictError, NotFoundError, StateBackend, TrackUpdate
from ..deps import get_read_state, get_write_state
from ..etag import compute_etag

router = APIRouter(prefix="/find-replace", tags=["find-replace"])

FindReplaceField = Literal["notes", "genre", "comments"]
FindReplaceMode = Literal["literal", "regex"]
_MAX_REGEX_PATTERN_CHARS = 256
_MAX_REGEX_INPUT_CHARS = 10_000
_MAX_STABLE_IDS = 100
_REGEX_REQUEST_TIMEOUT_SECONDS = 0.25
_NESTED_QUANTIFIER = regex.compile(r"\((?:[^()\\]|\\.)*[*+][^()]*\)[*+{]")
_BACKREFERENCE = regex.compile(r"\\[1-9]")


class FindReplaceScope(BaseModel):
    field: FindReplaceField = "notes"
    stable_ids: list[str] = Field(min_length=1, max_length=_MAX_STABLE_IDS)
    find: str = Field(min_length=1)
    replace: str = ""
    mode: FindReplaceMode = "literal"
    case_sensitive: bool = False

    @field_validator("stable_ids")
    @classmethod
    def _stable_ids_are_unique(cls, stable_ids: list[str]) -> list[str]:
        if len(stable_ids) != len(set(stable_ids)):
            raise ValueError("stable_ids must be unique")
        return stable_ids


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


def _compile(find: str, mode: FindReplaceMode, case_sensitive: bool) -> regex.Pattern:
    if mode == "regex" and (
        len(find) > _MAX_REGEX_PATTERN_CHARS
        or _NESTED_QUANTIFIER.search(find) is not None
        or _BACKREFERENCE.search(find) is not None
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsafe_regex",
                "message": "regex exceeds the safe complexity policy",
            },
        )
    pattern = find if mode == "regex" else regex.escape(find)
    flags = 0 if case_sensitive else regex.IGNORECASE
    try:
        return regex.compile(pattern, flags)
    except regex.error as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_regex", "message": str(exc)},
        ) from exc


def _new_value(
    current: str | None,
    pattern: regex.Pattern,
    replace: str,
    mode: FindReplaceMode,
    regex_deadline: float | None,
) -> str | None:
    if current is None:
        return None
    if len(current) > _MAX_REGEX_INPUT_CHARS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "input_too_large",
                "message": "value exceeds regex safety limit",
            },
        )
    try:
        if mode == "literal":
            return pattern.sub(replace, current)
        if regex_deadline is None:
            raise RuntimeError("regex substitution requires a request deadline")
        timeout = regex_deadline - time.monotonic()
        if timeout <= 0:
            raise TimeoutError
        return pattern.sub(replace, current, timeout=timeout)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsafe_regex",
                "message": "regex exceeded the safety time limit",
            },
        ) from exc


def _regex_deadline(mode: FindReplaceMode) -> float | None:
    if mode == "regex":
        return time.monotonic() + _REGEX_REQUEST_TIMEOUT_SECONDS
    return None


def _get_field(
    backend: StateBackend, stable_id: str, field: FindReplaceField
) -> tuple[object, str, str]:
    """Returns (track, current field value, etag). 404s propagate."""
    try:
        track = backend.get_track(stable_id)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": str(exc), "stable_id": stable_id},
        ) from exc
    etag = compute_etag(track.stable_id, track.updated_at, track.selection_tag)
    return track, getattr(track, field), etag


@router.post("/preview", response_model=FindReplacePreviewOut)
def preview(
    body: FindReplacePreviewIn,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> FindReplacePreviewOut:
    pattern = _compile(body.find, body.mode, body.case_sensitive)
    regex_deadline = _regex_deadline(body.mode)
    results: list[FindReplaceRowOut] = []
    match_count = 0
    for stable_id in body.stable_ids:
        _track, current, etag = _get_field(backend, stable_id, body.field)
        new_value = _new_value(
            current, pattern, body.replace, body.mode, regex_deadline
        )
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
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> FindReplaceApplyOut:
    missing_etags = [sid for sid in body.stable_ids if sid not in body.expected_etags]
    if missing_etags:
        raise HTTPException(
            status_code=422,
            detail={"error": "missing_expected_etag", "stable_ids": missing_etags},
        )

    pattern = _compile(body.find, body.mode, body.case_sensitive)
    regex_deadline = _regex_deadline(body.mode)

    to_apply: list[tuple[str, str]] = []  # (stable_id, new_value)
    skipped_noop: list[str] = []
    for stable_id in body.stable_ids:
        _track, current, _etag = _get_field(backend, stable_id, body.field)
        new_value = _new_value(
            current, pattern, body.replace, body.mode, regex_deadline
        )
        if new_value == current:
            skipped_noop.append(stable_id)
            continue
        to_apply.append((stable_id, new_value))

    replacement_by_id = dict(to_apply)
    updates = [
        TrackUpdate(
            stable_id,
            {body.field: replacement_by_id[stable_id]}
            if stable_id in replacement_by_id
            else {},
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
            new_value=getattr(track, body.field),
            etag=compute_etag(track.stable_id, track.updated_at, track.selection_tag),
        )
        for track in tracks
        if track.stable_id in replacement_by_id
    ]
    publish("library.changed", {"kind": "tracks", "ids": [r.stable_id for r in results]})

    return FindReplaceApplyOut(
        applied_count=len(results), skipped_noop=skipped_noop, results=results
    )


__all__ = ["router"]
