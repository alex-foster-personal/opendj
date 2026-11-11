"""HTTP twin of ``python -m apps.sync_hub.hash_pending_backfill`` (issue #11292).

    POST /api/v1/cloudsync/hash-pending-backfill  start ``run_for_hub_backfill``
    GET  /api/v1/cloudsync/hash-pending-backfill  poll progress

The POST kicks the same :func:`apps.sync_hub.hash_pending_backfill.run_for_hub_backfill`
the CLI calls onto a background thread so GET can report hashed vs remaining
without holding a request worker for the whole run. Local operator only.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.state.backfill_content_hash import BackfillReport
from apps.sync_hub import hash_pending_backfill
from apps.sync_hub.transport import SyncTransportError

from .cloudsync_ops import LOCAL_ONLY_RESPONSE, _refuse, require_local_operator
from .cloudsync_status import data_dir_for_request

router = APIRouter(prefix="/cloudsync", tags=["cloudsync"])
MOUNT_UNDER_API_PREFIX = True

logger = logging.getLogger(__name__)

THREAD_NAME = "cloudsync-hash-pending-backfill"


class HashPendingBackfillStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    live: bool = Field(
        default=True,
        description="When true, persist hashes locally and push to the hub (CLI --live).",
    )
    limit: int | None = Field(
        default=None,
        ge=0,
        description="Cap hub stable_ids processed this run (CLI --limit).",
    )
    hub_url: str | None = Field(
        default=None,
        min_length=1,
        pattern=r"^https?://",
        description="Override hub URL (CLI --hub-endpoint).",
    )


class HashPendingBackfillProgressOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: Literal["idle", "running", "done", "error"]
    live: bool = False
    total: int = Field(description="Hub hash_pending rows in this run.")
    processed: int = Field(description="Rows visited so far.")
    remaining: int = Field(description="total - processed while running or done.")
    hashed: int = 0
    resolvable: int = 0
    unresolvable: int = 0
    resolved_via_location: int = 0
    resolved_via_track_path: int = 0
    last_error: str | None = None


@dataclass
class _Flight:
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = False
    live: bool = False
    total: int = 0
    processed: int = 0
    report: BackfillReport | None = None
    last_error: str | None = None
    thread: threading.Thread | None = None


def _runner_for(state: Any) -> _Flight:
    flight = getattr(state, "hash_pending_backfill_flight", None)
    if isinstance(flight, _Flight):
        return flight
    flight = _Flight()
    state.hash_pending_backfill_flight = flight
    return flight


def _progress_snapshot(flight: _Flight) -> HashPendingBackfillProgressOut:
    with flight.lock:
        report = flight.report or BackfillReport(live=flight.live)
        if flight.running:
            state: Literal["idle", "running", "done", "error"] = "running"
        elif flight.last_error is not None:
            state = "error"
        elif flight.processed > 0 or flight.total > 0:
            state = "done"
        else:
            state = "idle"
        total = flight.total if flight.total else report.total
        processed = flight.processed
        remaining = max(0, total - processed) if total else 0
        return HashPendingBackfillProgressOut(
            state=state,
            live=flight.live,
            total=total,
            processed=processed,
            remaining=remaining,
            hashed=report.hashed,
            resolvable=report.resolvable,
            unresolvable=report.unresolvable,
            resolved_via_location=report.resolved_via_location,
            resolved_via_track_path=report.resolved_via_track_path,
            last_error=flight.last_error,
        )


def _on_progress_factory(flight: _Flight) -> hash_pending_backfill.ProgressCallback:
    def _on_progress(report: BackfillReport, processed: int) -> None:
        with flight.lock:
            if flight.total == 0:
                flight.total = report.total
            flight.processed = processed
            flight.report = BackfillReport(**dataclasses.asdict(report))

    return _on_progress


def _run_backfill(data_dir: Path, body: HashPendingBackfillStartIn, flight: _Flight) -> None:
    on_progress = _on_progress_factory(flight)
    report = BackfillReport(live=body.live)
    try:
        report = hash_pending_backfill.run_for_hub_backfill(
            data_dir,
            live=body.live,
            limit=body.limit,
            hub_endpoint=body.hub_url,
            on_progress=on_progress,
        )
    except (RuntimeError, SyncTransportError) as exc:
        with flight.lock:
            flight.last_error = str(exc)
        logger.warning("hash-pending backfill failed: %s", exc)
    finally:
        with flight.lock:
            flight.running = False
            flight.report = report
            flight.processed = report.total
            flight.total = report.total


BACKFILL_RESPONSES: dict[int | str, dict[str, Any]] = {
    **LOCAL_ONLY_RESPONSE,
    409: {
        "description": (
            "CLOUDSYNC_HASH_BACKFILL_RUNNING: a backfill is already running. "
            "CLOUDSYNC_HASH_BACKFILL_NO_AUDIO: hub pending rows exist but none "
            "resolve to audio on this machine."
        ),
    },
    502: {
        "description": "The hub was unreachable or answered with something unusable.",
    },
}


@router.get(
    "/hash-pending-backfill",
    response_model=HashPendingBackfillProgressOut,
    responses=LOCAL_ONLY_RESPONSE,
    dependencies=[Depends(require_local_operator)],
)
def get_hash_pending_backfill_progress(request: Request) -> HashPendingBackfillProgressOut:
    """Poll hashed vs remaining for the in-flight or last hub backfill."""
    return _progress_snapshot(_runner_for(request.app.state))


@router.post(
    "/hash-pending-backfill",
    response_model=HashPendingBackfillProgressOut,
    responses=BACKFILL_RESPONSES,
    dependencies=[Depends(require_local_operator)],
)
def start_hash_pending_backfill(
    body: HashPendingBackfillStartIn,
    request: Request,
) -> HashPendingBackfillProgressOut:
    """Start ``run_for_hub_backfill`` on a background thread."""
    data_dir = data_dir_for_request(request)
    flight = _runner_for(request.app.state)
    with flight.lock:
        if flight.running:
            raise _refuse(
                409,
                "CLOUDSYNC_HASH_BACKFILL_RUNNING",
                "a hash-pending backfill is already running; poll GET /api/v1/cloudsync/hash-pending-backfill.",
            )

    try:
        if not hash_pending_backfill.hub_pending_has_local_audio(
            data_dir,
            hub_endpoint=body.hub_url,
            limit=body.limit,
        ):
            raise _refuse(
                409,
                "CLOUDSYNC_HASH_BACKFILL_NO_AUDIO",
                "this machine resolves none of the hub hash_pending audio; run on a machine that holds the files.",
            )
    except SyncTransportError as exc:
        raise _refuse(502, "CLOUDSYNC_HUB_UNREACHABLE", str(exc)) from exc

    with flight.lock:
        flight.running = True
        flight.live = body.live
        flight.total = 0
        flight.processed = 0
        flight.report = BackfillReport(live=body.live)
        flight.last_error = None

    thread = threading.Thread(
        target=_run_backfill,
        args=(data_dir, body, flight),
        name=THREAD_NAME,
        daemon=True,
    )
    with flight.lock:
        flight.thread = thread
    thread.start()
    return _progress_snapshot(flight)


__all__ = [
    "HashPendingBackfillProgressOut",
    "HashPendingBackfillStartIn",
    "router",
]
