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

from apps.cloud import job as cloud_job
from apps.engine_core.jobs.runner import (
    UnknownJobKind,
    known_kinds,
    resolve_and_reenqueue,
    worker_argv,
)
from apps.engine_core.jobs.store import (
    ActiveIdOverlap,
    ActiveJob,
    DuplicateRule,
    JobConflict,
    JobDuplicate,
    JobNotFound,
)
from apps.shared.engine_app_state import (
    data_dir_from_engine_request,
    jobs_runner_from_engine_request,
    jobs_store_from_engine_request,
)
from apps.stems import job as stems_job
from apps.webui.server.routes import ingest as ingest_routes

router = APIRouter(prefix="/jobs", tags=["jobs"])

ANLZ_CACHE_REBUILD_KIND: str = "anlz.cache-rebuild"
"""Reserved kind for the ANLZ cache rebuild worker once it registers."""

FARM_JOB_KINDS: tuple[str, ...] = (
    stems_job.JOB_KIND,
    cloud_job.JOB_KIND,
    ANLZ_CACHE_REBUILD_KIND,
)

DEFAULT_LIST_LIMIT: int = 200
# An unbounded limit is a request to serialise the whole table into one
# response. Bounded at the edge so the refusal is a 422 naming the range,
# rather than a slow 200 nobody asked for.
MAX_LIST_LIMIT: int = 1000

# 409 code when a stems.separate request duplicates an active job; the
# detail carries that job as "job" so the caller adopts it.
STEMS_JOB_ACTIVE: str = "stems_job_active"
ANLZ_JOB_ACTIVE: str = "anlz_job_active"


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
    stderr_tail: list[str] | None = Field(
        default=None,
        description=(
            "the last lines the worker wrote to stderr, kept on every ending, "
            "success included (STEM-60)"
        ),
    )
    outcome: dict[str, Any] | None = Field(
        default=None,
        description=(
            "what the kind measured after its worker exited, e.g. stems "
            "bundles expected vs present (STEM-59); null for kinds without a check"
        ),
    )


class StemsJobActiveDetail(BaseModel):
    """The detail of a 409 refusing a duplicate stems.separate: the job to adopt."""

    code: Literal["stems_job_active"]
    message: str
    job: JobOut


class StemsJobActiveOut(BaseModel):
    """409 body of `POST /api/v1/jobs` when an active stems.separate already does the work."""

    detail: StemsJobActiveDetail


class AnlzJobActiveDetail(BaseModel):
    """The detail of a 409 refusing a duplicate anlz.cache_rebuild."""

    code: Literal["anlz_job_active"]
    message: str
    job: JobOut


class AnlzJobActiveOut(BaseModel):
    """409 body when an active anlz.cache_rebuild already does this work."""

    detail: AnlzJobActiveDetail


class JobReenqueueConflictOut(BaseModel):
    """409 body of a re-enqueue: a duplicate stems.separate (structured), or a
    row that cannot be re-enqueued in its state (a sentence)."""

    detail: StemsJobActiveDetail | str


class JobKindsOut(BaseModel):
    kinds: list[str]


@router.get("", response_model=list[JobOut])
def list_jobs(
    request: Request,
    limit: Annotated[
        int, Query(ge=1, le=MAX_LIST_LIMIT)
    ] = DEFAULT_LIST_LIMIT,
) -> list[dict[str, Any]]:
    return jobs_store_from_engine_request(request).list(limit=limit)


@router.get("/kinds", response_model=JobKindsOut)
def list_kinds() -> JobKindsOut:
    """The registered worker kinds. Empty until a kind registers one."""
    return JobKindsOut(kinds=list(known_kinds()))


def _resolved_stems_payload(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """``scope: pending`` becomes the enrich card's own "not yet" songs (STEM-58).

    Resolved HERE, at enqueue, through ``app.state.stems_scope_fn``, so the row
    records exactly which songs it was asked for and its outcome check can
    measure each one. A worker that resolved the scope itself used a second
    definition (rows, raw paths, one root) and could finish "done" without
    touching a single song the card counted.
    """
    from apps.stems.job import canonical_payload as canonical_stems_payload

    try:
        payload = canonical_stems_payload(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if "data_dir" not in payload:
        payload = {**payload, "data_dir": str(data_dir_from_engine_request(request))}
    if "scope" not in payload:
        try:
            worker_argv("stems.separate", payload)
        except (UnknownJobKind, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return payload
    if "data_dir" in payload:
        engine_dir = Path(ingest_routes.COVERAGE_DATA_DIR).resolve()
        requested = Path(payload["data_dir"]).resolve()
        if requested != engine_dir:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"scope pending is resolved against this engine's library ({engine_dir}); "
                    f"for data_dir {requested} pass explicit stable_ids instead"
                ),
            )
    scope_fn = getattr(request.app.state, "stems_scope_fn", None)
    if scope_fn is None:
        raise RuntimeError("engine stems scope is not mounted on app.state.stems_scope_fn")
    scope = scope_fn()
    if not scope.pending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"no songs need stems: of {scope.songs} songs with audio on this Mac, "
                f"{scope.done} have stems, {scope.terminal} cannot be made here and "
                f"{scope.failed} failed"
            ),
        )
    resolved = {key: value for key, value in payload.items() if key != "scope"}
    resolved["stable_ids"] = scope.pending_ids()
    resolved["resolved_from"] = {
        "scope": payload["scope"],
        "unit": "songs",
        "denominator": "present",
        "songs": scope.songs,
        "pending": len(scope.pending),
        "done": scope.done,
    }
    try:
        worker_argv("stems.separate", resolved)
    except (UnknownJobKind, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return resolved


def _active_stems_overlap_detail(job_id: str) -> str:
    """409 body for a pending scope that still names songs an active job owns (STEM-61)."""
    return (
        "a stems job is already queued or running for some of these songs "
        f"(job {job_id}); wait and submit scope: pending again "
        "(the next snapshot omits songs that now have bundles)"
    )


@router.post(
    "",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_409_CONFLICT: {
            "model": StemsJobActiveOut,
            "description": "an active stems.separate job already does this work",
        },
    },
)
def enqueue_job(request: Request, body: JobIn) -> dict[str, Any]:
    payload = body.payload
    block_active_ids: list[str] | None = None
    if body.kind == "stems.separate":
        from apps.engine_core.jobs.stems_dedup import covering_job

        had_scope = "scope" in payload
        payload = _resolved_stems_payload(request, payload)
        if had_scope:
            raw_ids = payload.get("stable_ids")
            if not isinstance(raw_ids, list) or not raw_ids:
                raise RuntimeError(
                    "resolved stems scope did not name stable_ids; refusing to enqueue"
                )
            block_active_ids = [item for item in raw_ids if isinstance(item, str) and item]
            if len(block_active_ids) != len(raw_ids):
                raise RuntimeError(
                    "resolved stems scope stable_ids are not strings; refusing to enqueue"
                )
        requested = payload
        default_data_dir = data_dir_from_engine_request(request)
        try:
            job, created = jobs_store_from_engine_request(
                request
            ).enqueue_unless_active(
                body.kind,
                payload,
                conflicts=lambda active: covering_job(
                    active, requested, default_data_dir=default_data_dir
                ),
                external_ref=body.external_ref,
                block_active_ids=block_active_ids,
            )
        except ActiveIdOverlap as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_active_stems_overlap_detail(exc.job_id),
            ) from exc
        if not created:
            raise _stems_job_active(job, "not enqueued again")
        return job
    if body.kind == "anlz.cache_rebuild":
        from apps.analysis import anlz_cache_job
        from apps.engine_core.jobs.anlz_cache_dedup import covering_job as anlz_covering

        default_data_dir = data_dir_from_engine_request(request)
        payload = anlz_cache_job.canonical_payload(
            payload, default_data_dir=default_data_dir
        )
        job, created = jobs_store_from_engine_request(request).enqueue_unless_active(
            body.kind,
            payload,
            conflicts=lambda active: anlz_covering(
                active, payload, default_data_dir=default_data_dir
            ),
            external_ref=body.external_ref,
        )
        if not created:
            raise _anlz_job_active(job, "not enqueued again")
        return job
    try:
        worker_argv(body.kind, payload)
    except UnknownJobKind as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return jobs_store_from_engine_request(request).enqueue(
        body.kind, payload, external_ref=body.external_ref
    )


@router.get("/{job_id}", response_model=JobOut)
def get_job(request: Request, job_id: str) -> dict[str, Any]:
    try:
        return jobs_store_from_engine_request(request).get(job_id)
    except JobNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
    try:
        return await jobs_runner_from_engine_request(request).cancel(job_id)
    except JobNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except JobConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc


def _anlz_job_active(job: dict[str, Any], outcome: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": ANLZ_JOB_ACTIVE,
            "message": (
                f"anlz.cache_rebuild job {job['id']} is already "
                f"{job['status']} for this work; {outcome}"
            ),
            "job": job,
        },
    )


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
    store = jobs_store_from_engine_request(request)
    try:
        job = store.get(job_id)
        conflicts = (
            _reenqueue_conflicts(job, data_dir_from_engine_request(request))
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
    "ANLZ_CACHE_REBUILD_KIND",
    "DEFAULT_LIST_LIMIT",
    "FARM_JOB_KINDS",
    "MAX_LIST_LIMIT",
    "JobIn",
    "AnlzJobActiveDetail",
    "AnlzJobActiveOut",
    "JobKindsOut",
    "JobOut",
    "JobReenqueueConflictOut",
    "StemsJobActiveDetail",
    "StemsJobActiveOut",
    "router",
]
