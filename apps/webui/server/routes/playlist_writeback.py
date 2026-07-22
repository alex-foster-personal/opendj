"""Playlist writeback endpoints -- LANE playlists-router, node
``write-back-rekordbox-djay``.

Pushes a webui-canonical playlist's membership into rekordbox / djay.
Business logic lives in :mod:`..playlist_writeback` (service layer, unit
tested without an HTTP client); this module is the thin FastAPI surface.

  * ``GET  /api/v1/playlists/{id}/writeback/capabilities``
    -> per-vendor ``{available, reason}``. Never fabricates availability --
    a vendor whose database can't be reached is reported unavailable with
    an explicit reason, not silently omitted.
  * ``GET  /api/v1/playlists/{id}/writeback/plan?vendor=rekordbox|djay``
    -> dry diff preview: ``added``/``removed``/``unresolved`` stable_ids
    plus whether the target playlist already exists. Read-only; never
    mutates the vendor database.
  * ``POST /api/v1/playlists/{id}/writeback/apply``
    body ``{vendor, dry_run=true, force_adopt=false}`` -> same shape as
    plan plus ``applied``/``error``. ``dry_run`` defaults true (rail 4 of
    the reused six-rail pattern); a live apply against an already-existing
    target additionally requires ``force_adopt=true`` (see the service
    module docstring for why).

Error contract mirrors the rest of the daemon
(``{"detail": {code, message}}``):

  * 404 (via the shared ``NotFoundError`` handler) -- unknown playlist_id.
  * 503 ``WRITEBACK_VENDOR_UNAVAILABLE`` -- the vendor's database isn't
    reachable (no master.plain.db / djay MediaLibrary.db copy on disk).
    ``apply`` is additionally gated behind ``get_write_state`` so a peer
    holding the cloud writer lock blocks it exactly like any other write.

NOTE for the wave integrator: wire with
``app.include_router(playlist_writeback_routes.router, prefix=api_prefix)``
in apps/webui/server/app.py (hotspot -- not edited here).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..backend import StateBackend
from ..deps import get_read_state, get_write_state
from ..playlist_writeback import (
    VENDORS,
    WritebackApplyResult,
    WritebackCapability,
    WritebackPlan,
    WritebackService,
    WritebackUnavailable,
)

router = APIRouter(prefix="/playlists", tags=["playlist-writeback"])

VendorLiteral = Literal["rekordbox", "djay"]


# ----------------------------------------------------------- schemas


class VendorCapabilityOut(BaseModel):
    vendor: str
    available: bool
    reason: Optional[str] = None


class WritebackCapabilitiesOut(BaseModel):
    playlist_id: str
    vendors: list[VendorCapabilityOut]


class WritebackPlanOut(BaseModel):
    playlist_id: str
    vendor: str
    playlist_name: str
    target_exists: bool
    added: list[str]
    removed: list[str]
    unresolved: list[str]
    is_noop: bool


class WritebackApplyIn(BaseModel):
    vendor: VendorLiteral
    dry_run: bool = True
    force_adopt: bool = False


class WritebackApplyOut(BaseModel):
    playlist_id: str
    vendor: str
    playlist_name: str
    applied: bool
    dry_run: bool
    added: list[str]
    removed: list[str]
    error: Optional[str] = None


# ----------------------------------------------------------- service dependency


def get_writeback_service(
    backend: StateBackend = Depends(get_read_state),  # noqa: ARG001 - forces read-state resolution first
) -> WritebackService:
    # Stateless (see ..playlist_writeback docstring): a fresh instance per
    # request is cheap and side-steps any app.state lifecycle wiring; tests
    # override this dependency directly with a service built from a fake
    # writer_factory.
    return WritebackService()


def _capability_out(cap: WritebackCapability) -> VendorCapabilityOut:
    return VendorCapabilityOut(vendor=cap.vendor, available=cap.available, reason=cap.reason)


def _plan_out(playlist_id: str, plan: WritebackPlan) -> WritebackPlanOut:
    return WritebackPlanOut(
        playlist_id=playlist_id,
        vendor=plan.vendor,
        playlist_name=plan.playlist_name,
        target_exists=plan.target_exists,
        added=plan.added,
        removed=plan.removed,
        unresolved=plan.unresolved,
        is_noop=plan.is_noop,
    )


def _apply_out(playlist_id: str, result: WritebackApplyResult) -> WritebackApplyOut:
    return WritebackApplyOut(
        playlist_id=playlist_id,
        vendor=result.vendor,
        playlist_name=result.playlist_name,
        applied=result.applied,
        dry_run=result.dry_run,
        added=result.added,
        removed=result.removed,
        error=result.error,
    )


def _unavailable(exc: WritebackUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail={
        "code": "WRITEBACK_VENDOR_UNAVAILABLE",
        "message": str(exc),
    })


# ----------------------------------------------------------- endpoints


@router.get(
    "/{playlist_id}/writeback/capabilities",
    response_model=WritebackCapabilitiesOut,
)
def get_writeback_capabilities(
    playlist_id: str,
    backend: StateBackend = Depends(get_read_state),
    service: WritebackService = Depends(get_writeback_service),
) -> WritebackCapabilitiesOut:
    backend.get_playlist(playlist_id)  # 404 via the shared NotFoundError handler
    caps = [service.capability(v) for v in VENDORS]
    return WritebackCapabilitiesOut(
        playlist_id=playlist_id,
        vendors=[_capability_out(c) for c in caps],
    )


@router.get("/{playlist_id}/writeback/plan", response_model=WritebackPlanOut)
def get_writeback_plan(
    playlist_id: str,
    vendor: VendorLiteral = Query(...),
    backend: StateBackend = Depends(get_read_state),
    service: WritebackService = Depends(get_writeback_service),
) -> WritebackPlanOut:
    playlist = backend.get_playlist(playlist_id)
    try:
        plan = service.plan(
            vendor=vendor, playlist_name=playlist.name,
            desired_ids=list(playlist.items),
        )
    except WritebackUnavailable as exc:
        raise _unavailable(exc) from exc
    return _plan_out(playlist_id, plan)


@router.post("/{playlist_id}/writeback/apply", response_model=WritebackApplyOut)
def apply_writeback(
    playlist_id: str,
    body: WritebackApplyIn,
    backend: StateBackend = Depends(get_write_state),
    service: WritebackService = Depends(get_writeback_service),
) -> WritebackApplyOut:
    playlist = backend.get_playlist(playlist_id)
    try:
        result = service.apply(
            vendor=body.vendor, playlist_name=playlist.name,
            desired_ids=list(playlist.items), dry_run=body.dry_run,
            force_adopt=body.force_adopt,
        )
    except WritebackUnavailable as exc:
        raise _unavailable(exc) from exc
    return _apply_out(playlist_id, result)


__all__ = [
    "WritebackApplyIn",
    "WritebackApplyOut",
    "WritebackCapabilitiesOut",
    "WritebackPlanOut",
    "get_writeback_service",
    "router",
]
