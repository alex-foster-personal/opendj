"""Transition memory over HTTP (MEMORY-02, MEMORY-03, ADR-0129). Mounted under ``/api/v1``.

  GET    /autodj/pair-transition?out=&in=[&out_deck=&in_deck=]  200 {plan, meta} or 404 pair_transition_not_found
  PUT    /autodj/pair-transition                 {plan, source, style} -> 200 {plan, meta}
  DELETE /autodj/pair-transition?out=&in=        204 or 404 pair_transition_not_found
  POST   /autodj/pair-transition/played          {out, in[, plan, source, style]} -> 200 {plan, meta}
  GET    /autodj/pair-transition/capture         200 what the hand-mix capture is doing, and how its last session ended

``out_deck`` and ``in_deck`` ask for the saved plan moved onto the decks the pair is loaded on now.
``POST /played`` is save-on-run: the executor sends the plan that just completed and it becomes the pair's default, or
sends no plan when the saved one replayed. Source or style without a plan is a 422 ``plan_invalid``.
A document that is not a plan is a 422 ``plan_invalid``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.autodj.http import (
    PairNotFound,
    PairTransition,
    PlanError,
    clear_pair_transition,
    get_pair_transition,
    on_decks,
    record_played,
    save_pair_transition,
)
from apps.shared.state import db as state_db

from .. import pair_capture
from ..deps import get_library_state_db
from ..errors import ErrorBody

router = APIRouter(prefix="/autodj", tags=["autodj"])
MOUNT_UNDER_API_PREFIX = True

Source = Literal["planner", "captured", "edited"]
_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorBody}}
_INVALID: dict[int | str, dict[str, Any]] = {422: {"model": ErrorBody}}


class PairTransitionMetaOut(BaseModel):
    out_track_id: str
    in_track_id: str
    source: Source
    style: str
    created_at: str
    updated_at: str
    times_played: int


class PairTransitionOut(BaseModel):
    plan: dict[str, Any]
    meta: PairTransitionMetaOut


class PairTransitionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: dict[str, Any]
    source: Source
    style: str = Field(min_length=1)


class PairPlayedIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    out_id: str = Field(alias="out", min_length=1)
    in_id: str = Field(alias="in", min_length=1)
    plan: dict[str, Any] | None = None
    source: Source | None = None
    style: str | None = Field(None, min_length=1)


class CaptureLastOut(BaseModel):
    status: str
    reason: str
    out: str
    in_: str = Field(alias="in")


class CaptureStatusOut(BaseModel):
    state: Literal["idle", "capturing"]
    out: str | None
    in_: str | None = Field(alias="in")
    samples: int
    last: CaptureLastOut | None


@contextmanager
def _write(path: Path) -> Iterator[sqlite3.Connection]:
    conn = state_db.open_rw(path)
    try:
        with state_db.write_unit(conn, "pair_transition"):
            yield conn
    finally:
        conn.close()


def _error(code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=code, content=ErrorBody(error=error, message=message).model_dump())


def _not_found(out_id: str, in_id: str) -> JSONResponse:
    return _error(
        status.HTTP_404_NOT_FOUND, "pair_transition_not_found", f"no transition saved for {out_id} into {in_id}"
    )


def _invalid(error: PlanError) -> JSONResponse:
    return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "plan_invalid", str(error))


def _answer(saved: PairTransition) -> dict[str, Any]:
    return saved.to_json()


@router.get("/pair-transition", response_model=PairTransitionOut, responses={**_NOT_FOUND, **_INVALID})
def get_pair(
    out: Annotated[str, Query(min_length=1)],
    in_id: Annotated[str, Query(alias="in", min_length=1)],
    out_deck: Annotated[int | None, Query(ge=1, le=4)] = None,
    in_deck: Annotated[int | None, Query(ge=1, le=4)] = None,
    path: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> dict[str, Any] | JSONResponse:
    if (out_deck is None) != (in_deck is None):
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "plan_invalid", "out_deck and in_deck come together")
    conn = state_db.open_rw(path)
    try:
        saved = get_pair_transition(conn, out, in_id)
    finally:
        conn.close()
    if saved is None:
        return _not_found(out, in_id)
    document = saved.to_json()
    if out_deck is not None and in_deck is not None:
        try:
            document["plan"] = on_decks(saved.plan, out_deck, in_deck)
        except PlanError as error:
            return _invalid(error)
    return document


@router.put("/pair-transition", response_model=PairTransitionOut, responses=_INVALID)
def put_pair(
    body: PairTransitionIn,
    path: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> dict[str, Any] | JSONResponse:
    try:
        with _write(path) as conn:
            return _answer(save_pair_transition(conn, body.plan, source=body.source, style=body.style))
    except PlanError as error:
        return _invalid(error)


@router.delete(
    "/pair-transition", status_code=status.HTTP_204_NO_CONTENT, response_class=Response, responses=_NOT_FOUND
)
def delete_pair(
    out: Annotated[str, Query(min_length=1)],
    in_id: Annotated[str, Query(alias="in", min_length=1)],
    path: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> Response:
    with _write(path) as conn:
        cleared = clear_pair_transition(conn, out, in_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT) if cleared else _not_found(out, in_id)


@router.post("/pair-transition/played", response_model=PairTransitionOut, responses={**_NOT_FOUND, **_INVALID})
def post_played(
    body: PairPlayedIn,
    path: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> dict[str, Any] | JSONResponse:
    try:
        with _write(path) as conn:
            return _answer(
                record_played(conn, body.out_id, body.in_id, plan=body.plan, source=body.source, style=body.style)
            )
    except PairNotFound:
        return _not_found(body.out_id, body.in_id)
    except PlanError as error:
        return _invalid(error)


@router.get("/pair-transition/capture", response_model=CaptureStatusOut, response_model_by_alias=True)
def get_capture(request: Request) -> dict[str, Any]:
    return pair_capture.capture_of(request.app).status()
