"""HTTP surface for fixing tracks whose metadata is jammed into the title (TFIX-01..06).

    GET    /api/v1/title-fix/config              config + masked key status
    PUT    /api/v1/title-fix/config              replace config (enabled, policy, cap, tag families)
    PUT    /api/v1/title-fix/key                 save the user's OpenRouter key (local operator only)
    DELETE /api/v1/title-fix/key                 forget the user's key (local operator only)
    POST   /api/v1/title-fix/scans               start a scan (all tracks, a limit, or stable_ids)
    GET    /api/v1/title-fix/scans               recent scans
    GET    /api/v1/title-fix/scans/{scan_id}     one scan's progress and cost
    GET    /api/v1/title-fix/proposals           filter by state/status/source/tag/scan/batch, paged
    POST   /api/v1/title-fix/apply               apply SELECTED proposals (one undoable batch)
    POST   /api/v1/title-fix/reject              reject selected proposals
    GET    /api/v1/title-fix/batches             recent apply batches
    POST   /api/v1/title-fix/batches/{id}/undo   restore originals for a batch

CLI twin: ``python -m apps.webui.title_fix_cli`` (same service functions). A scan only writes
proposals; tracks change only through apply, which goes through StateBackend.update_tracks.
Errors use the ``{"detail": {"code", "message"}}`` envelope; a missing key is a 409
``title_fix_key_missing`` (the assistant's KEY_MISSING pattern), never a silent fallback.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from apps.tags.http import (
    STORE_CFG,
    BatchRow,
    ConfigError,
    KeyStatus,
    ProposalFilter,
    ProposalRow,
    ScanRow,
    StorePaths,
    TitleFixConfig,
    clear_user_key,
    load_config,
    resolve_key,
    save_config,
    save_user_key,
)

from .. import title_fix_service as svc
from ..backend import BatchConflictError, NotFoundError, StateBackend
from ..deps import get_read_state, get_write_state, write_gate
from ..local_operator import local_operator_refusal
from ..state_paths import resolve_state_db_path

router = APIRouter(prefix="/title-fix", tags=["title-fix"])
MOUNT_UNDER_API_PREFIX = True

Policy = Literal["off", "safe_tier", "gate_approved"]
TagFamily = Literal["version", "context", "cover", "project"]
ProposalStateLit = Literal["pending", "applied", "rejected", "superseded", "stale", "undone", "noop"]
FixerStatus = Literal["clean", "fixed", "review", "not_music", "project", "unknown"]


# ----- models -----------------------------------------------------------------


class KeyStatusOut(BaseModel):
    configured: bool
    source: Literal["user", "env", "none"]
    hint: str | None


class TitleFixConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    auto_apply_policy: Policy
    spend_cap_usd: float = Field(ge=STORE_CFG.SPEND_CAP_MIN_USD, le=STORE_CFG.SPEND_CAP_MAX_USD)
    tag_families: list[TagFamily]


class TitleFixConfigOut(TitleFixConfigIn):
    key: KeyStatusOut


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(min_length=1, max_length=512)


class ScanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stable_ids: list[str] | None = Field(default=None, min_length=1, max_length=svc.CFG.MAX_SELECTION)
    limit: int | None = Field(default=None, ge=1)


class ScanOut(BaseModel):
    scan_id: str
    started_at: str
    finished_at: str | None
    state: Literal["running", "done", "capped", "failed"]
    requested: int
    processed: int
    cost_usd: float
    spend_cap_usd: float
    error: str | None
    auto_batch_id: str | None
    auto_apply_policy: str


class ProposalOut(BaseModel):
    proposal_id: str
    scan_id: str
    stable_id: str
    created_at: str
    original_title: str
    original_artist: str
    proposed_title: str
    proposed_artist: str
    original_artist_credit: str
    tags: list[str]
    fixer_status: str
    source: str
    confidence: float | None
    jev_kind: str
    notes: list[str]
    error: str
    cost_usd: float
    state: str
    batch_id: str | None
    tags_added: list[str]
    changes_title: bool
    changes_artist: bool


class ProposalsPage(BaseModel):
    items: list[ProposalOut]
    total: int
    limit: int
    offset: int
    counts_by_state: dict[str, int]


class SelectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_ids: list[str] = Field(min_length=1, max_length=svc.CFG.MAX_SELECTION)


class UndoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skip_conflicts: bool = False


class SkippedOut(BaseModel):
    proposal_id: str
    reason: str


class OutcomeOut(BaseModel):
    batch_id: str | None
    applied: list[str]
    skipped: list[SkippedOut]
    warning: str | None = None


class BatchOut(BaseModel):
    batch_id: str
    created_at: str
    kind: str
    policy: str
    proposal_count: int
    undone_at: str | None


# ----- helpers ----------------------------------------------------------------


def _refuse(code: str, message: str, status_code: int = 409, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def _ctx(request: Request, backend: StateBackend) -> svc.Ctx:
    try:
        return svc.Ctx.open(resolve_state_db_path(request), backend, write_gate(request.app))
    except ConfigError as exc:  # a malformed or newer sidecar store: the documented envelope, never an opaque 500
        raise _refuse("title_fix_config_invalid", str(exc), 422) from exc


def _config_out(config: TitleFixConfig, key: KeyStatus) -> TitleFixConfigOut:
    return TitleFixConfigOut(
        enabled=config.enabled,
        auto_apply_policy=config.auto_apply_policy,  # type: ignore[arg-type]
        spend_cap_usd=config.spend_cap_usd,
        tag_families=list(config.tag_families),  # type: ignore[arg-type]
        key=KeyStatusOut(configured=key.configured, source=key.source, hint=key.hint),
    )


def _scan_out(scan: ScanRow) -> ScanOut:
    return ScanOut(**scan.__dict__)


def _proposal_out(row: ProposalRow) -> ProposalOut:
    data = {k: v for k, v in row.__dict__.items() if k != "original_artists"}
    return ProposalOut(**data, changes_title=row.changes_title, changes_artist=row.changes_artist)


def _outcome_out(outcome: svc.Outcome) -> OutcomeOut:
    return OutcomeOut(
        batch_id=outcome.batch_id,
        applied=outcome.applied,
        skipped=[SkippedOut(**s) for s in outcome.skipped],
        warning=outcome.warning,
    )


def _batch_out(batch: BatchRow) -> BatchOut:
    return BatchOut(**batch.__dict__)


def _require_local_operator(request: Request) -> None:
    reason = local_operator_refusal(request)
    if reason is not None:
        raise _refuse("title_fix_local_only", f"the API key is local-operator only: {reason}", 403)


def _service_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except svc.TitleFixError as exc:
        raise _refuse(exc.code, exc.message, exc.status, **exc.extra) from exc
    except ConfigError as exc:
        raise _refuse("title_fix_config_invalid", str(exc), 422) from exc
    except NotFoundError as exc:
        raise _refuse("title_fix_track_not_found", f"{exc}; nothing in this batch was written", 404) from exc
    except BatchConflictError as exc:
        raise _refuse(
            "title_fix_track_conflict",
            "a track changed while the batch was written; nothing applied",
            conflicts=exc.conflicts,
        ) from exc


# ----- config + key -----------------------------------------------------------


@router.get("/config", response_model=TitleFixConfigOut)
def get_title_fix_config(request: Request) -> TitleFixConfigOut:
    paths = StorePaths(resolve_state_db_path(request))
    config = _service_call(load_config, paths)
    return _config_out(config, _service_call(resolve_key, paths)[1])


@router.put("/config", response_model=TitleFixConfigOut)
def put_title_fix_config(body: TitleFixConfigIn, request: Request) -> TitleFixConfigOut:
    paths = StorePaths(resolve_state_db_path(request))
    key_status = _service_call(resolve_key, paths)[
        1
    ]  # before saving: a broken key file must not follow a committed save
    config = _service_call(
        save_config,
        paths,
        TitleFixConfig(
            enabled=body.enabled,
            auto_apply_policy=body.auto_apply_policy,
            spend_cap_usd=body.spend_cap_usd,
            tag_families=tuple(body.tag_families),
        ),
    )
    return _config_out(config, key_status)


@router.put("/key", response_model=KeyStatusOut, responses={403: {"description": "not the local operator"}})
def put_title_fix_key(body: KeyIn, request: Request) -> KeyStatusOut:
    _require_local_operator(request)
    status_ = _service_call(save_user_key, StorePaths(resolve_state_db_path(request)), body.api_key)
    return KeyStatusOut(configured=status_.configured, source=status_.source, hint=status_.hint)


@router.delete("/key", response_model=KeyStatusOut, responses={403: {"description": "not the local operator"}})
def delete_title_fix_key(request: Request) -> KeyStatusOut:
    _require_local_operator(request)
    status_ = _service_call(clear_user_key, StorePaths(resolve_state_db_path(request)))
    return KeyStatusOut(configured=status_.configured, source=status_.source, hint=status_.hint)


# ----- scans ------------------------------------------------------------------


@router.post(
    "/scans",
    response_model=ScanOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        409: {"description": "disabled, key missing, scan running, or no state.db"},
        404: {"description": "a requested stable_id does not exist"},
    },
)
def start_title_fix_scan(
    body: ScanIn,
    request: Request,
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> ScanOut:
    """Start a scan over the whole library, the first ``limit`` tracks, or explicit ``stable_ids`` (not both: 422)."""
    return _scan_out(_service_call(svc.start_scan, _ctx(request, backend), body.stable_ids, body.limit))


@router.get("/scans", response_model=list[ScanOut])
def list_title_fix_scans(
    request: Request,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
    limit: int = Query(default=20, ge=1, le=200),
) -> list[ScanOut]:
    ctx = _ctx(request, backend)
    svc.expire_stale_scans(ctx)  # a dead scan must not read as running forever
    return [_scan_out(s) for s in ctx.store.list_scans(limit)]


@router.get("/scans/{scan_id}", response_model=ScanOut, responses={404: {"description": "unknown scan"}})
def get_title_fix_scan(
    scan_id: str,
    request: Request,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> ScanOut:
    ctx = _ctx(request, backend)
    svc.expire_stale_scans(ctx)  # a dead scan must not read as running forever
    try:
        return _scan_out(ctx.store.get_scan(scan_id))
    except KeyError as exc:
        raise _refuse("title_fix_scan_not_found", str(exc), 404) from exc


# ----- proposals --------------------------------------------------------------


@router.get("/proposals", response_model=ProposalsPage)
def list_title_fix_proposals(  # noqa: PLR0913, PLR0917  # FastAPI query params are the filter contract
    request: Request,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
    state: ProposalStateLit | Literal["any"] = Query(default="pending"),  # noqa: B008  # FastAPI DI
    fixer_status: FixerStatus | None = Query(default=None),  # noqa: B008  # FastAPI DI
    source: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    scan_id: str | None = Query(default=None),
    batch_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> ProposalsPage:
    store = _ctx(request, backend).store
    rows, total = store.list_proposals(
        ProposalFilter(
            state=None if state == "any" else state,
            fixer_status=fixer_status,
            source=source,
            tag=tag,
            scan_id=scan_id,
            batch_id=batch_id,
            limit=limit,
            offset=offset,
        )
    )
    return ProposalsPage(
        items=[_proposal_out(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
        counts_by_state=store.counts_by_state(),
    )


@router.post("/apply", response_model=OutcomeOut, responses={409: {"description": "a track changed mid-write"}})
def apply_title_fix_proposals(
    body: SelectionIn,
    request: Request,
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> OutcomeOut:
    return _outcome_out(_service_call(svc.apply_proposals, _ctx(request, backend), body.proposal_ids))


@router.post("/reject", response_model=OutcomeOut)
def reject_title_fix_proposals(
    body: SelectionIn,
    request: Request,
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> OutcomeOut:
    return _outcome_out(_service_call(svc.reject_proposals, _ctx(request, backend), body.proposal_ids))


@router.get("/batches", response_model=list[BatchOut])
def list_title_fix_batches(
    request: Request,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
    limit: int = Query(default=20, ge=1, le=200),
) -> list[BatchOut]:
    return [_batch_out(b) for b in _ctx(request, backend).store.list_batches(limit)]


@router.post(
    "/batches/{batch_id}/undo",
    response_model=OutcomeOut,
    responses={404: {"description": "unknown batch"}, 409: {"description": "conflict or already undone"}},
)
def undo_title_fix_batch(
    batch_id: str,
    body: UndoIn,
    request: Request,
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> OutcomeOut:
    return _outcome_out(
        _service_call(svc.undo_batch, _ctx(request, backend), batch_id, skip_conflicts=body.skip_conflicts)
    )


__all__ = ["router"]
