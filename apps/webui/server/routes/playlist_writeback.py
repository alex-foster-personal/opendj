"""HTTP contract for fail-closed vendor playlist writeback."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from apps.shared.events import publish
from apps.shared.rekordbox_writeback import require_writeback_enabled

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state
from ..playlist_writeback import (
    VENDORS,
    WritebackConflict,
    WritebackService,
    WritebackUnavailable,
    WriterFactory,
    bind_default_writer_factory,
    default_writer_factory,
)

router = APIRouter(prefix="/playlists", tags=["playlist-writeback"])
VendorLiteral = Literal["rekordbox", "djay"]
TargetModeLiteral = Literal["live"]


class VendorCapabilityOut(BaseModel):
    vendor: str
    available: bool
    target_mode: TargetModeLiteral
    target_path: str | None = None
    reason: str | None = None


class WritebackCapabilitiesOut(BaseModel):
    playlist_id: str
    vendors: list[VendorCapabilityOut]


class WritebackTargetOut(BaseModel):
    playlist_id: str
    name: str


class WritebackTargetsOut(BaseModel):
    vendor: VendorLiteral
    target_mode: TargetModeLiteral
    target_path: str
    targets: list[WritebackTargetOut]


class WritebackPlanOut(BaseModel):
    playlist_id: str
    vendor: VendorLiteral
    target_mode: TargetModeLiteral
    target_path: str
    target_id: str
    target_name: str
    source_revision: str
    target_revision: str
    mapping_revision: str
    ordered_match: bool
    plan_token: str
    added: list[str]
    removed: list[str]
    unresolved: list[str]
    is_noop: bool


class WritebackApplyIn(BaseModel):
    vendor: VendorLiteral
    target_mode: TargetModeLiteral
    target_path: str
    target_id: str
    plan_token: str
    dry_run: bool = True
    confirmed: bool = False


class WritebackApplyOut(BaseModel):
    playlist_id: str
    vendor: VendorLiteral
    target_id: str
    target_name: str
    applied: bool
    dry_run: bool
    added: list[str]
    removed: list[str]
    backup_id: str | None = None
    target_revision: str | None = None
    error: str | None = None


class WritebackRollbackIn(BaseModel):
    vendor: VendorLiteral
    target_mode: TargetModeLiteral
    target_path: str
    target_id: str
    # uuid4().hex and nothing else: this value is joined into a path under
    # data/writeback-backups, so an unconstrained string containing ``../``
    # reads a file of the caller's choosing. Refused as 422 before the handler
    # runs; writeback_backup._require_backup_id re-checks at the join itself.
    backup_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    expected_target_revision: str
    confirmed: bool = False


class WritebackRollbackOut(BaseModel):
    playlist_id: str
    vendor: VendorLiteral
    target_id: str
    backup_id: str
    rolled_back: bool
    target_revision: str


def get_writeback_service(backend: StateBackend = Depends(get_read_state)) -> WritebackService:  # noqa: B008  # FastAPI DI
    source_lock_factory = getattr(backend, "hold_writeback_source_lock", None)
    if not callable(source_lock_factory):
        source_lock_factory = None
    state_db_path = getattr(backend, "writeback_state_db_path", None)
    writer_factory: WriterFactory = default_writer_factory
    if source_lock_factory is not None and isinstance(state_db_path, (str, Path)):
        writer_factory = bind_default_writer_factory(state_db_path)
    return WritebackService(
        writer_factory=writer_factory,
        source_members_reader=lambda playlist_id: list(backend.get_playlist(playlist_id).items),
        source_lock_factory=source_lock_factory,
    )


def _unavailable(exc: WritebackUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail={"code": "WRITEBACK_VENDOR_UNAVAILABLE", "message": str(exc)})


def _conflict(exc: WritebackConflict) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": "WRITEBACK_PLAN_CONFLICT", "message": str(exc)})


@router.get("/{playlist_id}/writeback/capabilities", response_model=WritebackCapabilitiesOut)
def get_writeback_capabilities(
    playlist_id: str, backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
    service: WritebackService = Depends(get_writeback_service),  # noqa: B008  # FastAPI DI
) -> WritebackCapabilitiesOut:
    backend.get_playlist(playlist_id)
    return WritebackCapabilitiesOut(playlist_id=playlist_id, vendors=[VendorCapabilityOut(**cap.__dict__) for cap in (service.capability(v) for v in VENDORS)])


@router.get("/{playlist_id}/writeback/targets", response_model=WritebackTargetsOut)
def get_writeback_targets(
    playlist_id: str, vendor: VendorLiteral = Query(...), target_mode: TargetModeLiteral = Query(...),  # noqa: B008  # FastAPI DI
    target_path: str = Query(...), backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
    service: WritebackService = Depends(get_writeback_service),  # noqa: B008  # FastAPI DI
) -> WritebackTargetsOut:
    backend.get_playlist(playlist_id)
    try:
        targets = service.targets(vendor=vendor, target_mode=target_mode, target_path=target_path)
    except WritebackUnavailable as exc:
        raise _unavailable(exc) from exc
    return WritebackTargetsOut(vendor=vendor, target_mode=target_mode, target_path=target_path,
        targets=[WritebackTargetOut(playlist_id=t.playlist_id, name=t.name) for t in targets])


@router.get("/{playlist_id}/writeback/plan", response_model=WritebackPlanOut)
def get_writeback_plan(
    playlist_id: str, vendor: VendorLiteral = Query(...), target_mode: TargetModeLiteral = Query(...),  # noqa: B008  # FastAPI DI
    target_path: str = Query(...), target_id: str = Query(...),
    backend: StateBackend = Depends(get_read_state), service: WritebackService = Depends(get_writeback_service),  # noqa: B008  # FastAPI DI
) -> WritebackPlanOut:
    playlist = backend.get_playlist(playlist_id)
    try:
        plan = service.plan(vendor=vendor, source_playlist_id=playlist_id, desired_ids=list(playlist.items),
            target_mode=target_mode, target_path=target_path, target_id=target_id)
    except WritebackUnavailable as exc:
        raise _unavailable(exc) from exc
    except WritebackConflict as exc:
        raise _conflict(exc) from exc
    return WritebackPlanOut(playlist_id=playlist_id, **plan.__dict__, is_noop=plan.is_noop)


@router.post("/{playlist_id}/writeback/apply", response_model=WritebackApplyOut)
def apply_writeback(
    playlist_id: str, body: WritebackApplyIn, backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
    service: WritebackService = Depends(get_writeback_service),  # noqa: B008  # FastAPI DI
) -> WritebackApplyOut:
    if body.vendor == "rekordbox":
        require_writeback_enabled("http.playlists.writeback.apply")
    playlist = backend.get_playlist(playlist_id)
    try:
        from apps.webui.server.playlist_writeback import WritebackTarget

        result = service.apply(
            vendor=body.vendor,
            source_playlist_id=playlist_id,
            desired_ids=list(playlist.items),
            target=WritebackTarget(body.target_mode, body.target_path, body.target_id),
            plan_token=body.plan_token,
            dry_run=body.dry_run,
            confirmed=body.confirmed,
        )
    except WritebackUnavailable as exc:
        raise _unavailable(exc) from exc
    except WritebackConflict as exc:
        raise _conflict(exc) from exc
    if not body.dry_run:
        publish("library.changed", {"kind": "playlists", "ids": [playlist_id]})
    return WritebackApplyOut(playlist_id=playlist_id, **result.__dict__)


@router.post("/{playlist_id}/writeback/rollback", response_model=WritebackRollbackOut)
def rollback_writeback(
    playlist_id: str, body: WritebackRollbackIn, backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
    service: WritebackService = Depends(get_writeback_service),  # noqa: B008  # FastAPI DI
) -> WritebackRollbackOut:
    # DELIBERATELY NOT GATED (mapped as gated=False). Rollback only exists
    # after a gated apply already wrote, so refusing it here would trap the
    # user with a bad write and no undo. It cannot be aimed anywhere: the
    # preimage is minted only by that apply, backup_id is pattern-bound above,
    # the target path is pinned to the canonical live DB, and the service
    # CAS-checks current membership. See apps/shared/rekordbox_writeback.py,
    # "RECOVERY PATHS ARE NOT GATED".
    backend.get_playlist(playlist_id)
    try:
        result = service.rollback(**body.model_dump())
    except WritebackUnavailable as exc:
        raise _unavailable(exc) from exc
    except WritebackConflict as exc:
        raise _conflict(exc) from exc
    publish("library.changed", {"kind": "playlists", "ids": [playlist_id]})
    return WritebackRollbackOut(playlist_id=playlist_id, **result.__dict__)


__all__ = ["get_writeback_service", "router"]
