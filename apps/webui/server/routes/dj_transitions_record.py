"""Why AutoPlay chose a track, and how it sounded (ADR-0139). Mounted under ``/api/v1``. Pure logging.

  GET  /dj/transitions/last[?pair=<out>:<in>]  200 the newest stored decision (or this pair's), with the pair's
                                               played meta and verdict, or 404 decision_not_found
  POST /dj/transitions/verdict                 {out, in, verdict: good|meh|bad} -> 200 {out_track_id, in_track_id, verdict}, or 404
                                               pair_transition_not_found when nothing has run between the pair

The decision is written by ``POST /dj/transition/decide``; the verdict lands in ``pair_verdicts`` beside the pair's ``pair_transitions`` row.
Nothing in the planner reads either.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.autodj.pair_memory import PairNotFound, last_decision, set_verdict
from apps.shared.state import db as state_db

from ..deps import get_library_state_db
from ..errors import ErrorBody

router = APIRouter(prefix="/dj/transitions", tags=["dj-transition"])

Verdict = Literal["good", "meh", "bad"]


class DecisionRecordOut(BaseModel):
    out_track_id: str
    #: Empty when the decision chose no track (its reason says why).
    in_track_id: str
    decision: dict[str, Any]
    decided_at: str
    #: The pair's saved plan meta once the transition has run, else null.
    played: dict[str, Any] | None
    verdict: Verdict | None
    verdict_at: str | None


class VerdictIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    out_id: str = Field(alias="out", min_length=1)
    in_id: str = Field(alias="in", min_length=1)
    verdict: Verdict


class VerdictOut(BaseModel):
    out_track_id: str
    in_track_id: str
    verdict: Verdict


@contextmanager
def _write(path: Path) -> Iterator[sqlite3.Connection]:
    conn = state_db.open_rw(path)
    try:
        with state_db.write_unit(conn, "pair_decision"):
            yield conn
    finally:
        conn.close()


def _error(code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=code, content=ErrorBody(error=error, message=message).model_dump())


def _pair(text: str | None) -> tuple[str, str] | None:
    if text is None:
        return None
    out_id, sep, in_id = text.partition(":")
    if not sep or not out_id:
        raise ValueError(f"pair must be <out>:<in>, got {text!r}")
    return out_id, in_id


@router.get("/last", response_model=DecisionRecordOut, responses={404: {"model": ErrorBody}, 422: {"model": ErrorBody}})
def get_last(
    pair: str | None = Query(None, description="<out stable id>:<in stable id>; omitted = the newest decision"),
    state_db_path: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> dict[str, Any] | JSONResponse:
    try:
        wanted = _pair(pair)
    except ValueError as error:
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "pair_invalid", str(error))
    conn = state_db.open_rw(state_db_path)
    try:
        record = last_decision(conn, wanted)
    finally:
        conn.close()
    if record is None:
        return _error(
            status.HTTP_404_NOT_FOUND, "decision_not_found", f"no AutoPlay decision stored for {pair or 'any pair'}"
        )
    return record


@router.post("/verdict", response_model=VerdictOut, responses={404: {"model": ErrorBody}})
def post_verdict(
    body: VerdictIn,
    state_db_path: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
) -> dict[str, Any] | JSONResponse:
    try:
        with _write(state_db_path) as conn:
            set_verdict(conn, body.out_id, body.in_id, body.verdict)
    except PairNotFound as error:
        return _error(status.HTTP_404_NOT_FOUND, "pair_transition_not_found", str(error))
    return {"out_track_id": body.out_id, "in_track_id": body.in_id, "verdict": body.verdict}
