"""In-app stems and lyrics job ordering over HTTP (PERFBATCH-05, #1865).

Every control has a matching CLI subcommand
(``python -m apps.analysis.queue_cli user-*``) and a UI surface
(``LibraryJobQueuePanel``). Missing endpoints fail the PR.

    POST /api/v1/library-jobs/enqueue
    GET  /api/v1/library-jobs
    PATCH /api/v1/library-jobs/{lane}/{stable_id}
    POST /api/v1/library-jobs/{lane}/{stable_id}/cancel

-Claude
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from apps.analysis import queue_user
from apps.analysis import store as analysis_store
from apps.analysis.queue import QueueError
from apps.analysis.queue_user import UserJobConflict
from apps.analysis.queue_user_lanes import MAX_ENQUEUE_IDS, USER_JOB_LANES
from apps.shared.events import publish
from apps.shared.paths import STATE_DB

from ..backend import StateBackend
from ..deps import get_write_state

router = APIRouter(prefix="/library-jobs", tags=["library-jobs"])

Lane = Literal["stems", "lyrics"]
Placement = Literal["next", "tail"]


class LibraryJobEnqueueIn(BaseModel):
    lane: Lane
    stable_ids: list[str] = Field(min_length=1, max_length=MAX_ENQUEUE_IDS)
    placement: Placement = "next"


class LibraryJobItemOut(BaseModel):
    lane: str
    stable_id: str
    state: str
    reason: str | None
    detail: str | None
    position: int
    attempts: int
    enqueued_at: str
    started_at: str | None
    finished_at: str | None


class LibraryJobEnqueueOut(BaseModel):
    lane: str
    items: list[LibraryJobItemOut]
    already_running: list[str]


class LibraryJobListOut(BaseModel):
    lane: str
    items: list[LibraryJobItemOut]
    counts: dict[str, int]


class LibraryJobReorderIn(BaseModel):
    before_stable_id: str | None = None


def _db_path(request: Request) -> Path:
    return Path(getattr(request.app.state, "analysis_db_path", STATE_DB))


def _data_dir(request: Request) -> Path:
    db = _db_path(request)
    if db.name == "state.db" and db.parent.name == "state":
        return db.parent.parent
    return db.parent


def _stems_root(request: Request) -> Path:
    roots = getattr(request.app.state, "stem_roots", None)
    if roots:
        return Path(roots[0])
    return _data_dir(request) / "state" / "stems"


def _open(request: Request) -> sqlite3.Connection:
    conn = analysis_store.open_conn(_db_path(request))
    queue_user.ensure_user_schema(conn)
    return conn


def _item_out(item: queue_user.UserJobItem) -> LibraryJobItemOut:
    return LibraryJobItemOut(
        lane=item.lane,
        stable_id=item.stable_id,
        state=item.state,
        reason=item.reason,
        detail=item.detail,
        position=item.position,
        attempts=item.attempts,
        enqueued_at=item.enqueued_at,
        started_at=item.started_at,
        finished_at=item.finished_at,
    )


def _http_error(exc: QueueError) -> HTTPException:
    status = 409 if isinstance(exc, UserJobConflict) else 422
    code = "conflict" if status == 409 else "queue_refused"
    if "no " in str(exc) and "job for" in str(exc):
        status = 404
        code = "no_such_job"
    return HTTPException(status_code=status, detail={"code": code, "message": str(exc)})


@router.post("/enqueue", response_model=LibraryJobEnqueueOut, status_code=201)
def enqueue_library_jobs(
    request: Request,
    body: LibraryJobEnqueueIn,
    _write_guard: StateBackend = Depends(get_write_state),  # noqa: B008
) -> LibraryJobEnqueueOut:
    conn = _open(request)
    try:
        result = queue_user.enqueue_next(
            conn,
            lane=body.lane,
            stable_ids=body.stable_ids,
            placement=body.placement,
            stems_root=_stems_root(request),
            data_dir=_data_dir(request),
        )
    except QueueError as exc:
        raise _http_error(exc) from exc
    finally:
        conn.close()
    publish(
        "library.changed",
        {"kind": "library_jobs", "ids": [i.stable_id for i in result.items]},
    )
    return LibraryJobEnqueueOut(
        lane=result.lane,
        items=[_item_out(i) for i in result.items],
        already_running=list(result.already_running),
    )


@router.get("", response_model=LibraryJobListOut)
def list_library_jobs(
    request: Request,
    lane: Lane = Query(description="stems or lyrics"),  # noqa: B008
    include: str | None = Query(
        default=None, description="settled to include terminals"
    ),
) -> LibraryJobListOut:
    if lane not in USER_JOB_LANES:
        raise HTTPException(
            status_code=422,
            detail={"code": "unknown_lane", "message": f"unknown lane {lane}"},
        )
    conn = _open(request)
    try:
        items = queue_user.list_lane(
            conn, lane, include_settled=(include == "settled")
        )
        counts = queue_user.counts_for_lane(conn, lane)
    finally:
        conn.close()
    return LibraryJobListOut(
        lane=lane, items=[_item_out(i) for i in items], counts=counts
    )


@router.patch("/{lane}/{stable_id}", response_model=LibraryJobItemOut)
def reorder_library_job(
    request: Request,
    lane: Lane,
    stable_id: str,
    body: LibraryJobReorderIn,
    _write_guard: StateBackend = Depends(get_write_state),  # noqa: B008
) -> LibraryJobItemOut:
    conn = _open(request)
    try:
        item = queue_user.reorder_item(
            conn,
            lane=lane,
            stable_id=stable_id,
            before_stable_id=body.before_stable_id,
        )
    except QueueError as exc:
        raise _http_error(exc) from exc
    finally:
        conn.close()
    publish("library.changed", {"kind": "library_jobs", "ids": [item.stable_id]})
    return _item_out(item)


@router.post("/{lane}/{stable_id}/cancel", response_model=LibraryJobItemOut)
def cancel_library_job(
    request: Request,
    lane: Lane,
    stable_id: str,
    _write_guard: StateBackend = Depends(get_write_state),  # noqa: B008
) -> LibraryJobItemOut:
    conn = _open(request)
    try:
        item = queue_user.cancel_item(conn, lane=lane, stable_id=stable_id)
    except QueueError as exc:
        raise _http_error(exc) from exc
    finally:
        conn.close()
    publish("library.changed", {"kind": "library_jobs", "ids": [item.stable_id]})
    return _item_out(item)
