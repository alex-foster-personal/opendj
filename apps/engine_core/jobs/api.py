"""Jobs HTTP surface.

Agent-native parity: everything the UI can do to a job has an endpoint here,
so an agent drives the identical flow. Refusals are real status codes with
the reason in the body -- 400 unknown kind, 404 no such job, 409 the row is
not in a state that permits the transition.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from apps.engine_core.jobs.runner import (
    JobRunner,
    UnknownJobKind,
    known_kinds,
    resolve_and_reenqueue,
    worker_argv,
)
from apps.engine_core.jobs.store import (
    ActiveJob,
    DuplicateRule,
    JobConflict,
    JobDuplicate,
    JobNotFound,
    JobStore,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])

DEFAULT_LIST_LIMIT: int = 200
# An unbounded limit is a request to serialise the whole table into one
# response. Bounded at the edge so the refusal is a 422 naming the range,
# rather than a slow 200 nobody asked for.
MAX_LIST_LIMIT: int = 1000

# 409 code when a stems.separate request duplicates an active job; the
# detail carries that job as "job" so the caller adopts it.
STEMS_JOB_ACTIVE: str = "stems_job_active"


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


class StemsJobActiveDetail(BaseModel):
    """The detail of a 409 refusing a duplicate stems.separate: the job to adopt."""

    code: Literal["stems_job_active"]
    message: str
    job: JobOut


class StemsJobActiveOut(BaseModel):
    """409 body of `POST /api/v1/jobs` when an active stems.separate already does the work."""

    detail: StemsJobActiveDetail


class JobReenqueueConflictOut(BaseModel):
    """409 body of a re-enqueue: a duplicate stems.separate (structured), or a
    row that cannot be re-enqueued in its state (a sentence)."""

    detail: StemsJobActiveDetail | str


class JobKindsOut(BaseModel):
    kinds: list[str]


def _store(request: Request) -> JobStore:
    store = getattr(request.app.state, "jobs_store", None)
    if store is None:
        raise RuntimeError(
            "engine jobs store is not mounted on app.state.jobs_store"
        )
    return store


def _engine_data_dir(request: Request) -> Path:
    """The engine's own data dir: the library a payload with no ``data_dir``
    separates (the worker inherits it as ``MDT_DATA_DIR``)."""
    cfg = getattr(request.app.state, "engine_cfg", None)
    if cfg is None:
        raise RuntimeError(
            "engine config is not mounted on app.state.engine_cfg; the "
            "stems.separate duplicate rule cannot tell which library a "
            "payload without data_dir names, and it will not guess"
        )
    return Path(cfg.data_dir)


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


@router.post(
    "",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_409_CONFLICT: {
            "model": StemsJobActiveOut,
            "description": "an active stems.separate job already does this work",
        }
    },
)
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
        from apps.engine_core.jobs.stems_dedup import covering_job
        from apps.stems.job import canonical_payload as canonical_stems_payload

        payload = canonical_stems_payload(payload)
        # A separation is paid GPU time and the worker does not dedupe, so a
        # second request for work an active job already does is refused
        # atomically with the insert, and the caller gets that job to adopt.
        requested = payload
        default_data_dir = _engine_data_dir(request)
        job, created = _store(request).enqueue_unless_active(
            body.kind,
            payload,
            conflicts=lambda active: covering_job(
                active, requested, default_data_dir=default_data_dir
            ),
            external_ref=body.external_ref,
        )
        if not created:
            raise _stems_job_active(job, "not enqueued again")
        return job
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


def _stems_job_active(job: dict[str, Any], outcome: str) -> HTTPException:
    """The 409 a duplicate stems.separate request gets: the active job, to adopt."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": STEMS_JOB_ACTIVE,
            "message": (
                f"stems.separate job {job['id']} is already "
                f"{job['status']} for this work; {outcome}"
            ),
            "job": job,
        },
    )


def _reenqueue_conflicts(
    job: dict[str, Any], default_data_dir: Path
) -> DuplicateRule | None:
    """The duplicate rule a re-enqueue of ``job`` must pass, or None.

    Only stems.separate has one, and it is the fresh enqueue's rule. A row's
    payload never changes, so reading it before the store's transaction is
    safe; the active-row check itself runs inside that transaction. The
    payload is canonicalized again with today's policy, as the runner will
    when it launches the row, so the rule compares what the re-run will
    actually separate at (Codex on #6819). A payload that cannot run under
    today's policy has no rule: its launch fails by name.
    """
    if job["kind"] != "stems.separate":
        return None
    from apps.engine_core.jobs.stems_dedup import covering_job
    from apps.stems.job import StemsJobPayloadError
    from apps.stems.job import canonical_payload as canonical_stems_payload

    try:
        requested = canonical_stems_payload(job["payload"])
    except StemsJobPayloadError:
        return None

    def _conflicts(active: list[ActiveJob]) -> str | None:
        return covering_job(active, requested, default_data_dir=default_data_dir)

    return _conflicts


@router.post(
    "/{job_id}/reenqueue",
    response_model=JobOut,
    responses={
        status.HTTP_409_CONFLICT: {
            "model": JobReenqueueConflictOut,
            "description": "another active stems.separate job does this work, or the row cannot be re-enqueued",
        }
    },
)
def reenqueue_job(request: Request, job_id: str) -> dict[str, Any]:
    store = _store(request)
    try:
        job = store.get(job_id)
        conflicts = (
            _reenqueue_conflicts(job, _engine_data_dir(request))
            if job["kind"] == "stems.separate"
            else None
        )
        return resolve_and_reenqueue(store, job_id, conflicts=conflicts)
    except JobNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except JobDuplicate as exc:
        raise _stems_job_active(exc.existing, "not re-enqueued") from exc
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
    "JobReenqueueConflictOut",
    "StemsJobActiveDetail",
    "StemsJobActiveOut",
    "router",
]
