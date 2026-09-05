"""Analyze-on-import surface: the rekordbox-unmapped analysis queue.

A track imported straight into the local library gets a ``tracks`` row and no
live ``rekordbox`` ``track_vendor_ids`` twin, so there is no rekordbox ANLZ to
read a beatgrid, waveform or cue out of. A mapping to another vendor (djay,
serato) is not a substitute: it supplies no ANLZ, so such a track is still
work; see :data:`apps.analysis.backlog.ANLZ_VENDOR`.

:mod:`apps.analysis` can supply all three, but only once a row exists to
serve, and nothing ran it on import. This router is the queue and the run
trigger for that gap:

  * ``GET  /analysis-queue``      -> what still needs analyzing, plus the live
    progress of the drain and the state of the auto-drain
  * ``POST /analysis-queue/run``  -> start the drain now

Deliberately a thin facade. The queue itself is
:func:`apps.analysis.backlog.scan`, and the drain is the SAME one-slot job the
"Refresh analysis" button already owns
(``POST /ingest/refresh {"scope": "unmapped"}``), so:

  * there is never a second analysis job racing the first, and
  * the existing TopBar progress popover renders the auto-drain's progress
    with no new UI, because it polls that job's status.

Read this endpoint rather than ``/ingest/coverage`` when the question is "can
a LOCAL track get a beatgrid yet". Coverage answers the whole-library
question and counts rekordbox-mapped tracks, which have an ANLZ already.

Requirements (mini-PRD):
  ✔︎ ✅ GET /analysis-queue: queue + denominators + job progress + auto state.
    [if] a track carries a live rekordbox mapping [then] it is not in the
    queue; a djay- or serato-only mapping leaves it queued
    [if] ``limit`` is passed [then] only ``items`` shrinks; ``pending`` still
    reports the real queue size
    [if] ``limit`` is < 1 or > MAX_ITEMS [then ⛔️] 422
  ✔︎ ✅ POST /analysis-queue/run: start the unmapped-scope drain.
    [if] any refresh job is already running [then ⛔️] 409
    [if] the analysis step is disabled in the ingest config [then ⛔️] 422
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ..analysis_autostart import AutoAnalyzeState
from . import ingest

router = APIRouter(prefix="/analysis-queue", tags=["analysis"])

MAX_ITEMS: int = 1000
DEFAULT_ITEMS: int = 200
ANALYSIS_KINDS: frozenset[str] = frozenset({
    "vocals", "beatgrid", "key", "cues", "waveform", "phrase", "loudness", "stems", "other"
})


class AnalysisQueueItemOut(BaseModel):
    """One queued track. ``file_path`` is what the runner decodes.

    Named apart from ``models.QueueItemOut`` (the M3 triage queues) on
    purpose: two Pydantic models sharing a name collapse into one OpenAPI
    schema, and the loser is served under the winner's shape.
    """

    stable_id: str
    file_path: str
    title: str | None


class AutoAnalyzeOut(BaseModel):
    """State of the daemon's auto-drain reconcile loop."""

    enabled: bool
    interval_s: float
    attempts: int
    last_started_at: float | None
    last_signature: str | None
    last_outcome: str | None


class AnalysisQueueOut(BaseModel):
    """``analyzed + unreachable + pending`` always equals ``unmapped``."""

    unmapped: int
    pending: int
    analyzed: int
    unreachable: int
    signature: str
    items: list[AnalysisQueueItemOut]
    job: ingest.RefreshStatusOut
    auto: AutoAnalyzeOut


class AnalysisOrderOut(BaseModel):
    stable_id: str
    kind: str
    phase: str


class AnalysisOrdersOut(BaseModel):
    items: list[AnalysisOrderOut]


def _auto_state(request: Request) -> AutoAnalyzeState:
    return request.app.state.auto_analyze


@router.get("", response_model=AnalysisQueueOut)
def get_analysis_queue(
    request: Request,
    limit: int = Query(
        DEFAULT_ITEMS, ge=1, le=MAX_ITEMS,
        description="Cap on listed items. Never caps the reported counts.",
    ),
) -> AnalysisQueueOut:
    """The analyze-on-import queue, its denominators and the drain's progress."""
    queue = ingest.unmapped_backlog(limit=limit)
    auto = _auto_state(request)
    return AnalysisQueueOut(
        unmapped=queue.unmapped,
        pending=queue.pending_total,
        analyzed=queue.analyzed,
        unreachable=queue.unreachable,
        signature=queue.signature,
        items=[
            AnalysisQueueItemOut(
                stable_id=i.stable_id, file_path=i.file_path, title=i.title
            )
            for i in queue.pending
        ],
        job=ingest.refresh_status(),
        auto=AutoAnalyzeOut(
            enabled=auto.enabled,
            interval_s=auto.interval_s,
            attempts=auto.attempts,
            last_started_at=auto.last_started_at,
            last_signature=auto.last_signature,
            last_outcome=auto.last_outcome,
        ),
    )


@router.post("/run", response_model=ingest.RefreshStatusOut, status_code=202)
def run_analysis_queue() -> ingest.RefreshStatusOut:
    """Drain the queue now, through the shared one-slot refresh job."""
    return ingest.start_refresh(ingest.RefreshIn(scope="unmapped"))


@router.post("/orders/{stable_id}/{kind}", response_model=AnalysisOrderOut, status_code=202)
def order_track_analysis(stable_id: str, kind: str) -> AnalysisOrderOut:
    """Order one real analysis CLI run through the same single refresh slot."""
    if kind not in ANALYSIS_KINDS:
        raise HTTPException(422, f"unknown analysis kind {kind!r}")
    ingest.start_refresh(ingest.RefreshIn(scope="track", stable_id=stable_id, analysis_kind=kind))
    return AnalysisOrderOut(stable_id=stable_id, kind=kind, phase="queued")


@router.get("/orders/{stable_id}", response_model=AnalysisOrdersOut)
def get_track_analysis_orders(stable_id: str) -> AnalysisOrdersOut:
    """Current shared-job state for a track, readable by UI and HTTP agents."""
    job = ingest._JOBS.current
    if job is None or stable_id not in job.analysis_orders:
        return AnalysisOrdersOut(items=[])
    return AnalysisOrdersOut(
        items=[AnalysisOrderOut(
            stable_id=stable_id,
            kind=job.analysis_orders[stable_id],
            phase=job.phase,
        )]
    )


__all__ = [
    "DEFAULT_ITEMS",
    "MAX_ITEMS",
    "AnalysisOrderOut",
    "AnalysisOrdersOut",
    "AnalysisQueueItemOut",
    "AnalysisQueueOut",
    "AutoAnalyzeOut",
    "get_analysis_queue",
    "get_track_analysis_orders",
    "order_track_analysis",
    "router",
    "run_analysis_queue",
]
