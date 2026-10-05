"""Standalone HTTP parity for safe Pioneer USB export workflow.

The feature orchestrator intentionally does not register this router in the
shared ``app.py`` hotspot. Integration must include it under ``/api/v1``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from apps.feature_flags import FlagRefusal
from apps.shared.rekordbox_writeback import require_writeback_enabled
from apps.webui.server.routes.usb_gate import usb_export_gate

router = APIRouter(prefix="/usb-export", tags=["usb-export"])


class PlaylistModel(BaseModel):
    name: str
    track_ids: list[int] = Field(default_factory=list)
    parent_id: int | None = None


class TrackUpdateModel(BaseModel):
    id: int
    title: str | None = None
    rating: int | None = None
    bpmx100: int | None = None
    dj_comment: str | None = None
    color_id: int | None = None


class PlanRequest(BaseModel):
    template_path: str
    target_root: str
    playlists: list[PlaylistModel] = Field(default_factory=list)
    track_updates: list[TrackUpdateModel] = Field(default_factory=list)


class PlanModel(BaseModel):
    plan_id: str
    schema_version: int
    scope: str
    template_path: str
    template_sha256: str
    target_root: str
    volume_label: str
    volume_uuid: str
    authorization_id: str
    output_relative_path: str
    playlists: list[PlaylistModel]
    track_updates: list[TrackUpdateModel]


class ApplyRequest(BaseModel):
    plan: PlanModel
    confirmation: str


class ReceiptModel(BaseModel):
    plan_id: str
    volume_uuid: str
    output_relative_path: str
    output_sha256: str
    playlist_ids: list[int]
    verified: bool


class ReadbackRequest(BaseModel):
    plan: PlanModel
    receipt: ReceiptModel


class ReadbackModel(BaseModel):
    plan_id: str
    volume_uuid: str
    output_relative_path: str
    output_sha256: str
    playlists: list[dict[str, Any]]
    track_updates: list[dict[str, Any]]
    verified: bool


def _error(exc) -> JSONResponse:
    if exc.code == "platform_unsupported":
        status_code = 503
    elif exc.code.endswith(("_invalid", "_mismatch")):
        status_code = 422
    else:
        status_code = 409
    return JSONResponse(status_code=status_code, content={"detail": exc.to_dict()})


def _disabled_response(refusal: FlagRefusal | None) -> JSONResponse:
    """SAND-01/SAND-02: the same fourth refusal ``/api/v1/flags`` reports.

    A route that never read ``app.state.feature_flags`` stayed callable under
    the appstore profile even though the flag it exists to gate says off;
    this is the response that closes that gap. ``refusal`` is None when the
    flag is off for a LOCAL reason (a plain override, an explicit
    ``MDT_FEATURE_FLAGS_FILE``) rather than the shipped store profile or an
    actually-sandboxed runtime, in which case the response must not blame
    Apple's sandbox for a decision this machine made on its own.
    """
    detail: dict[str, str | None]
    if refusal is not None:
        detail = {
            "code": refusal.code,
            "message": refusal.message,
            "ui_title": refusal.ui_title,
        }
    else:
        detail = {
            "code": "usb_export_disabled_in_this_build",
            "message": "USB export is turned off by this build's own configuration.",
            "ui_title": None,
        }
    return JSONResponse(status_code=503, content={"detail": detail})


@router.post("/plan", response_model=PlanModel)
def plan_export(body: PlanRequest, request: Request) -> dict[str, Any] | JSONResponse:
    from apps.sync.usb.pioneer import export_workflow as workflow
    from apps.sync.usb.pioneer.writer_onelibrary import PlaylistSpec, TrackUpdate

    gate = usb_export_gate(request)
    if not gate.available:
        return _disabled_response(gate.refusal)
    try:
        plan = workflow.plan_export(
            template_path=body.template_path,
            target_root=body.target_root,
            playlists=tuple(
                PlaylistSpec(
                    name=item.name,
                    track_ids=tuple(item.track_ids),
                    parent_id=item.parent_id,
                )
                for item in body.playlists
            ),
            track_updates=tuple(
                TrackUpdate(**item.model_dump()) for item in body.track_updates
            ),
        )
    except workflow.UsbExportError as exc:
        return _error(exc)
    return plan.to_dict()


@router.post("/apply", response_model=ReceiptModel)
def apply_export(
    body: ApplyRequest, request: Request
) -> dict[str, Any] | JSONResponse:
    # usb.export is checked FIRST so a disabled store build reports the SAME
    # refusal on apply as on plan/readback; checking the writeback gate first
    # would report the one-way-import refusal instead, a different reason for
    # the same disabled capability (SAND-01 review round 2, PR #1668).
    gate = usb_export_gate(request)
    if not gate.available:
        return _disabled_response(gate.refusal)
    from apps.sync.usb.pioneer import export_workflow as workflow

    require_writeback_enabled("http.usb-export.apply")
    try:
        plan = workflow.ExportPlan.from_dict(body.plan.model_dump())
        receipt = workflow.apply_export(plan, confirmation=body.confirmation)
    except workflow.UsbExportError as exc:
        return _error(exc)
    return receipt.to_dict()


@router.post("/readback", response_model=ReadbackModel)
def readback_export(
    body: ReadbackRequest, request: Request
) -> dict[str, Any] | JSONResponse:
    from apps.sync.usb.pioneer import export_workflow as workflow

    gate = usb_export_gate(request)
    if not gate.available:
        return _disabled_response(gate.refusal)
    try:
        plan = workflow.ExportPlan.from_dict(body.plan.model_dump())
        receipt = workflow.ApplyReceipt.from_dict(body.receipt.model_dump())
        report = workflow.readback_export(plan, receipt)
    except workflow.UsbExportError as exc:
        return _error(exc)
    return report.to_dict()
