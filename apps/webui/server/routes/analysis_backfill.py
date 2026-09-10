"""The native-analysis v1 backfill queue over HTTP (NATIVE-10, agent parity).

Four controls, one per queue verb, and every one of them has a matching CLI
subcommand (:mod:`apps.analysis.queue_cli`) and a UI control (the Backfill
Queue panel at ``/analysis-backfill``). That triple is the repo's
agent-native parity rule and spec section 4's own line, "Every UI control
(toggle, queue, re-analyze) has a CLI and an HTTP endpoint":

    POST /api/v1/analysis/backfill/enqueue   enqueue   queue_cli enqueue
    GET  /api/v1/analysis/backfill/progress  progress  queue_cli progress
    POST /api/v1/analysis/backfill/cancel    cancel    queue_cli cancel
    POST /api/v1/analysis/backfill/resume    resume    queue_cli resume
    GET  /api/v1/analysis/backfill/batches   list      queue_cli list

Not to be confused with ``/api/v1/analysis-queue`` (routes/analysis_queue.py),
which is the DERIVED rekordbox-unmapped backlog: a projection of tracks +
analysis with no state of its own, and no cancel or resume, because it has
nothing to cancel. This router is the persisted v1 queue.

Draining is deliberately NOT an endpoint. A drain holds a process pool for
minutes to hours; running it inside the daemon's request worker would block
the event loop and die with the request. The runner is
``python -m apps.analysis.queue_cli run``, and this router is how an agent or
the UI plans, watches and steers it.

Requirements (mini-PRD):
  ✔︎ ✅ POST /analysis/backfill/enqueue: admit, plan, persist.
    [if] a track is longer than 90 minutes [then] it comes back as a refused
    item with its named reason, never absent from the response
    [if] no stable_ids are given [then ⛔️] 422
    [if] a stable_id is not in the library [then ⛔️] 422 naming it
  ✔︎ ✅ GET /analysis/backfill/progress: aggregate + per-item + the plan.
    [if] the batch does not exist [then ⛔️] 404
    [if] nothing has run [then] counts are zeros, never an empty object
  ✔︎ ✅ POST /analysis/backfill/cancel: pending AND in-flight.
    [if] the batch is already done [then] it cancels 0 items and says so
  ✔︎ ✅ POST /analysis/backfill/resume: cancelled and abandoned items return.
    [if] items already completed [then] they are NOT re-queued

-Claude
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from apps.analysis import queue as queue_api
from apps.analysis import queue_store, queue_targets
from apps.analysis import store as analysis_store
from apps.shared.paths import STATE_DB

from ..backend import StateBackend
from ..deps import get_write_state

router = APIRouter(prefix="/analysis/backfill", tags=["analysis"])

#: Cap on listed items in one progress response. The COUNTS are never capped.
MAX_ITEMS: int = 1000
DEFAULT_ITEMS: int = 200


class BackfillMemoryModelOut(BaseModel):
    """The measured peak-RSS model a batch was budgeted under.

    Carried on every response rather than assumed, because the floor and
    slope are MEASURED numbers for one producer version. A report that says
    "budgeted at 330 MB + 68 MB/min" without saying where that was measured
    is quoting a remembered figure.
    """

    backend: str = Field(description="Producer the model was measured on")
    producer_version: str = Field(description="Version it was measured at")
    floor_mb: float = Field(description="Predicted peak RSS at zero length, MB")
    slope_mb_per_min: float = Field(description="Extra peak RSS per audio minute, MB")
    measured_on: str = Field(description="Host, method and date of the measurement")
    source: str = Field(description="Document the measurement is recorded in")


class BackfillItemOut(BaseModel):
    """One (track, lane) of work and its state.

    Named apart from ``routes/queues.py``'s ``QueueItemOut`` (the M3 triage
    queues) for the reason its neighbour ``routes/analysis_queue.py`` states:
    two Pydantic models sharing a name collapse into ONE OpenAPI schema and
    the loser is served under the winner's shape. That collision really
    happened here on the first openapi dump, and it is silent -- the schema
    still validates, it just describes the wrong thing.
    """

    stable_id: str
    lane: str
    backend: str
    state: str = Field(
        description=(
            "pending, running, done, skipped, failed, refused or cancelled"
        )
    )
    reason: str | None = Field(
        default=None,
        description="Named cause for a refusal, a failure, or a skip",
    )
    attempts: int = Field(description="Claims so far; a kill-and-resume shows 2")
    duration_s: float | None = Field(
        default=None, description="Audio length the admission rule budgeted with"
    )
    predicted_peak_mb: float | None = Field(
        default=None, description="floor + slope x minutes under the batch model"
    )


class BackfillBatchSummaryOut(BaseModel):
    """One batch without its items."""

    batch_id: str
    state: str = Field(description="queued, running, cancelled or done")
    workers: int = Field(
        description=(
            "Concurrency the admission rule chose from the LONGEST ADMITTED "
            "track, never from core count"
        )
    )
    band: str = Field(
        description="under_20_min, 20_to_45_min, over_45_min, or empty"
    )
    created_at: str
    note: str | None = None
    counts: dict[str, int]


class BackfillBatchListOut(BaseModel):
    batches: list[BackfillBatchSummaryOut]


class BackfillEnqueueIn(BaseModel):
    stable_ids: list[str] = Field(description="Tracks to (re)analyze")
    lane: str = Field(description="beatgrid, key, waveform, loudness or vocal")
    backend: str = Field(description="Producer to run, own_<lane>.<producer>")
    note: str | None = Field(default=None, description="Why this batch exists")


class BackfillEnqueueOut(BaseModel):
    """The plan. ``offered = admitted + refused``, always."""

    batch_id: str
    offered: int
    admitted: int
    refused: int
    workers: int
    band: str
    memory_model: BackfillMemoryModelOut


class BackfillProgressOut(BaseModel):
    """Aggregate + per-item progress. ``counts`` carries every state's zero."""

    batch_id: str
    state: str
    workers: int
    band: str
    memory_model: dict[str, object]
    counts: dict[str, int]
    total: int
    settled: int = Field(description="done + skipped + failed + refused")
    created_at: str
    updated_at: str
    items: list[BackfillItemOut]


class BackfillBatchIn(BaseModel):
    batch_id: str


class BackfillCancelOut(BaseModel):
    batch_id: str
    cancelled: int = Field(description="Open items (pending and in flight) cancelled")


class BackfillResumeOut(BaseModel):
    batch_id: str
    requeued: int = Field(description="Items put back in the queue")
    workers: int = Field(description="Concurrency re-planned from what is LEFT")
    band: str
    counts: dict[str, int]


def _db_path(request: Request) -> Path:
    return Path(getattr(request.app.state, "analysis_db_path", STATE_DB))


def _open(request: Request) -> sqlite3.Connection:
    """Read-write, with the full analysis schema.

    Every route here reads or writes the queue tables, and a GET on a daemon
    that has never run the queue would otherwise 500 on a missing table
    rather than answer "no batches".
    """
    conn = analysis_store.open_conn(_db_path(request))
    queue_store.ensure_queue_tables(conn)
    return conn


def _item_out(item: queue_store.QueueItem) -> BackfillItemOut:
    return BackfillItemOut(
        stable_id=item.stable_id,
        lane=item.lane,
        backend=item.backend,
        state=item.state,
        reason=item.reason,
        attempts=item.attempts,
        duration_s=item.duration_s,
        predicted_peak_mb=item.predicted_peak_mb,
    )


@router.post("/enqueue", response_model=BackfillEnqueueOut, status_code=201)
def enqueue_backfill(
    request: Request,
    body: BackfillEnqueueIn,
    # Mutating route: the same cross-host write exclusion every other one
    # answers to. The returned backend is unused; the guard is the point.
    # `Depends(...)` in a default is the FastAPI DI idiom and ruff's B008 does
    # not special-case it. Suppressed per line, as cloudsync.py already does,
    # rather than by a repo-wide ruff config change: the same pattern sits
    # unsuppressed in 25 other route modules, so that config decision belongs
    # to whichever lane owns pyproject.toml's ruff block, not this one.
    _write_guard: StateBackend = Depends(get_write_state),  # noqa: B008
) -> BackfillEnqueueOut:
    """Plan a batch: admission rule, worker count, band, refusals by name."""
    if not body.stable_ids:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "no_targets",
                "message": "enqueue needs at least one stable_id",
            },
        )
    conn = _open(request)
    try:
        candidates = queue_targets.candidates_from_state(
            conn, body.stable_ids, lane=body.lane, backend=body.backend
        )
        result = queue_api.enqueue(conn, candidates, note=body.note)
    except queue_api.QueueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "queue_refused", "message": str(exc)},
        ) from exc
    finally:
        conn.close()
    return BackfillEnqueueOut(
        batch_id=result.batch_id,
        offered=result.offered,
        admitted=result.admitted,
        refused=result.refused,
        workers=result.workers,
        band=result.band,
        memory_model=BackfillMemoryModelOut(
            backend=result.model.backend,
            producer_version=result.model.producer_version,
            floor_mb=result.model.floor_mb,
            slope_mb_per_min=result.model.slope_mb_per_min,
            measured_on=result.model.measured_on,
            source=result.model.source,
        ),
    )


@router.get("/progress", response_model=BackfillProgressOut)
def get_backfill_progress(
    request: Request,
    batch_id: str = Query(description="Batch to report on"),
    limit: int = Query(
        DEFAULT_ITEMS, ge=0, le=MAX_ITEMS,
        description="Cap on listed items. Never caps the reported counts.",
    ),
) -> BackfillProgressOut:
    conn = _open(request)
    try:
        prog = queue_api.progress(conn, batch_id, item_limit=limit)
    except queue_api.QueueError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "no_such_batch", "message": str(exc)},
        ) from exc
    finally:
        conn.close()
    return BackfillProgressOut(
        batch_id=prog.batch_id,
        state=prog.state,
        workers=prog.workers,
        band=prog.band,
        memory_model=prog.memory_model,
        counts=prog.counts,
        total=prog.total,
        settled=prog.settled,
        created_at=prog.created_at,
        updated_at=prog.updated_at,
        items=[_item_out(i) for i in prog.items],
    )


@router.get("/batches", response_model=BackfillBatchListOut)
def list_backfill_batches(
    request: Request,
    limit: int = Query(20, ge=1, le=200, description="Newest first"),
) -> BackfillBatchListOut:
    conn = _open(request)
    try:
        batches = queue_store.list_batches(conn, limit=limit)
        return BackfillBatchListOut(
            batches=[
                BackfillBatchSummaryOut(
                    batch_id=b.batch_id,
                    state=b.state,
                    workers=b.workers,
                    band=b.band,
                    created_at=b.created_at,
                    note=b.note,
                    counts=queue_store.counts_by_state(conn, b.batch_id),
                )
                for b in batches
            ]
        )
    finally:
        conn.close()


@router.post("/cancel", response_model=BackfillCancelOut)
def cancel_backfill(
    request: Request,
    body: BackfillBatchIn,
    _write_guard: StateBackend = Depends(get_write_state),  # noqa: B008
) -> BackfillCancelOut:
    """Cancel pending AND in-flight items. Terminal ones are left alone."""
    conn = _open(request)
    try:
        cancelled = queue_api.cancel(conn, body.batch_id)
    except queue_api.QueueError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "no_such_batch", "message": str(exc)},
        ) from exc
    finally:
        conn.close()
    return BackfillCancelOut(batch_id=body.batch_id, cancelled=cancelled)


@router.post("/resume", response_model=BackfillResumeOut)
def resume_backfill(
    request: Request,
    body: BackfillBatchIn,
    _write_guard: StateBackend = Depends(get_write_state),  # noqa: B008
) -> BackfillResumeOut:
    """Put cancelled and abandoned items back; re-plan from what is LEFT."""
    conn = _open(request)
    try:
        requeued = queue_api.resume(conn, body.batch_id)
        prog = queue_api.progress(conn, body.batch_id, item_limit=0)
    except queue_api.QueueError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "no_such_batch", "message": str(exc)},
        ) from exc
    finally:
        conn.close()
    return BackfillResumeOut(
        batch_id=body.batch_id,
        requeued=requeued,
        workers=prog.workers,
        band=prog.band,
        counts=prog.counts,
    )


__all__ = [
    "DEFAULT_ITEMS",
    "MAX_ITEMS",
    "BackfillBatchIn",
    "BackfillBatchListOut",
    "BackfillBatchSummaryOut",
    "BackfillCancelOut",
    "BackfillEnqueueIn",
    "BackfillEnqueueOut",
    "BackfillItemOut",
    "BackfillMemoryModelOut",
    "BackfillProgressOut",
    "BackfillResumeOut",
    "cancel_backfill",
    "enqueue_backfill",
    "get_backfill_progress",
    "list_backfill_batches",
    "resume_backfill",
    "router",
]
