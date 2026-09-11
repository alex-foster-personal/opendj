"""Jobs HTTP surface.

Agent-native parity: everything the UI can do to a job has an endpoint here,
so an agent drives the identical flow. Refusals are real status codes with
the reason in the body -- 400 unknown kind, 404 no such job, 409 the row is
not in a state that permits the transition.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from apps.engine_core.jobs.runner import (
    JobRunner,
    UnknownJobKind,
    known_kinds,
    resolve_and_reenqueue,
    worker_argv,
)
from apps.engine_core.jobs.store import JobConflict, JobNotFound, JobStore

router = APIRouter(prefix="/jobs", tags=["jobs"])

DEFAULT_LIST_LIMIT: int = 200
# An unbounded limit is a request to serialise the whole table into one
# response. Bounded at the edge so the refusal is a 422 naming the range,
# rather than a slow 200 nobody asked for.
MAX_LIST_LIMIT: int = 1000


class JobIn(BaseModel):
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    external_ref: str | None = None


class JobOut(BaseModel):
    id: str
    kind: str
    payload: dict[str, Any]
    status: str
    progress: float
    message: str | None = None
    error: str | None = None
    attempt: int
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    owner_pid: int | None = None
    owner_boot_id: str
    worker_pid: int | None = None
    worker_pgid: int | None = None
    worker_argv: list[str] | None = None
    worker_started_at: float | None = None
    external_ref: str | None = None


class JobKindsOut(BaseModel):
    kinds: list[str]


def _store(request: Request) -> JobStore:
    store = getattr(request.app.state, "jobs_store", None)
    if store is None:
        raise RuntimeError(
            "engine jobs store is not mounted on app.state.jobs_store"
        )
    return store


def _runner(request: Request) -> JobRunner:
    runner = getattr(request.app.state, "jobs_runner", None)
    if runner is None:
        raise RuntimeError(
            "engine jobs runner is not mounted on app.state.jobs_runner"
        )
    return runner


@router.get("", response_model=list[JobOut])
def list_jobs(
    request: Request,
    limit: Annotated[
        int, Query(ge=1, le=MAX_LIST_LIMIT)
    ] = DEFAULT_LIST_LIMIT,
) -> list[dict[str, Any]]:
    return _store(request).list(limit=limit)


@router.get("/kinds", response_model=JobKindsOut)
def list_kinds() -> JobKindsOut:
    """The registered worker kinds. Empty until a kind registers one."""
    return JobKindsOut(kinds=list(known_kinds()))


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def enqueue_job(request: Request, body: JobIn) -> dict[str, Any]:
    # ValueError alongside UnknownJobKind: a registered builder rejecting its
    # payload is the same class of caller error as naming a kind that does
    # not exist, and letting it out as a 500 told an agent to retry a request
    # that will never succeed.
    payload = body.payload
    try:
        worker_argv(body.kind, payload)
    except (UnknownJobKind, ValueError) as exc:
        # The kind knows the shape of its own payload and this one is wrong.
        # That is the caller's mistake, so it is a 400 carrying the kind's own
        # sentence -- not a 500, which reads as "the engine broke" and sends
        # the caller looking in entirely the wrong place. The refusal lands
        # BEFORE the insert, so a bad payload never becomes a queued row.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    if body.kind == "stems.separate":
        from apps.stems.job import canonical_payload as canonical_stems_payload

        payload = canonical_stems_payload(payload)
    return _store(request).enqueue(
        body.kind, payload, external_ref=body.external_ref
    )


@router.get("/{job_id}", response_model=JobOut)
def get_job(request: Request, job_id: str) -> dict[str, Any]:
    try:
        return _store(request).get(job_id)
    except JobNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
    try:
        return await _runner(request).cancel(job_id)
    except JobNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except JobConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc


@router.post("/{job_id}/reenqueue", response_model=JobOut)
def reenqueue_job(request: Request, job_id: str) -> dict[str, Any]:
    try:
        return resolve_and_reenqueue(_store(request), job_id)
    except JobNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except JobConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "MAX_LIST_LIMIT",
    "JobIn",
    "JobKindsOut",
    "JobOut",
    "router",
]
