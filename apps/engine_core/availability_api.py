"""HTTP surface for engine-owned availability probing."""
from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, status
from pydantic import BaseModel, Field

from apps.engine_core.library_availability import (
    AvailabilityWorkerStatus,
    LibraryAvailabilityWorker,
)

AVAILABILITY_WORKER_ATTR: str = "availability_worker"
PROBE_PATH: str = "/api/v1/availability/probe"
STATUS_PATH: str = "/api/v1/availability/status"


class AvailabilityProbeIn(BaseModel):
    stable_ids: list[str] | None = None
    full: bool = False
    allow_mass_missing: bool = False


class AvailabilityProbeOut(BaseModel):
    accepted: bool = True
    phase: Literal["idle", "queued", "running", "complete", "refused", "failed"]


class AvailabilityStatusOut(BaseModel):
    phase: Literal["idle", "queued", "running", "complete", "refused", "failed"]
    pending: int = Field(description="Tracks still needing a probe pass")
    unknown: int = Field(description="Live tracks with no availability row")
    stale: int = Field(
        description="Rows whose tracks.updated_at is newer than checked_at"
    )
    awaiting_volume: int = Field(
        description="Rows under an unmounted /Volumes path to re-check"
    )
    processed_total: int = Field(
        description="Rows classified across this worker lifetime"
    )
    present: int = Field(description="Current present count in track_availability")
    last_error: str | None = None
    complete: bool = Field(
        description="True when no unknown/stale/awaiting_volume rows remain"
    )


def _status_out(status: AvailabilityWorkerStatus) -> AvailabilityStatusOut:
    payload = status.as_dict()
    return AvailabilityStatusOut.model_validate(payload)


def add_availability_routes(app: FastAPI, worker: LibraryAvailabilityWorker) -> None:
    setattr(app.state, AVAILABILITY_WORKER_ATTR, worker)

    @app.post(
        PROBE_PATH,
        response_model=AvailabilityProbeOut,
        tags=["library"],
        name="availability_probe",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def availability_probe(body: AvailabilityProbeIn | None = None) -> AvailabilityProbeOut:
        req = body or AvailabilityProbeIn()
        worker.request_probe(
            req.stable_ids,
            full=req.full,
            allow_mass_missing=req.allow_mass_missing,
        )
        snapshot = worker.status()
        return AvailabilityProbeOut(accepted=True, phase=snapshot.phase)

    @app.get(
        STATUS_PATH,
        response_model=AvailabilityStatusOut,
        tags=["library"],
        name="availability_status",
    )
    def availability_status() -> AvailabilityStatusOut:
        return _status_out(worker.status())


__all__ = [
    "AVAILABILITY_WORKER_ATTR",
    "AvailabilityProbeIn",
    "AvailabilityProbeOut",
    "AvailabilityStatusOut",
    "PROBE_PATH",
    "STATUS_PATH",
    "add_availability_routes",
]
