"""Standalone HTTP parity for safe Pioneer USB export workflow.

The feature orchestrator intentionally does not register this router in the
shared ``app.py`` hotspot. Integration must include it under ``/api/v1``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from apps.shared.rekordbox_writeback import require_writeback_enabled
from apps.sync.usb.pioneer import export_workflow as workflow
from apps.sync.usb.pioneer.writer_rbox import PlaylistSpec, TrackUpdate

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


def _error(exc: workflow.UsbExportError) -> JSONResponse:
    if exc.code == "platform_unsupported":
        status_code = 503
    elif exc.code.endswith(("_invalid", "_mismatch")):
        status_code = 422
    else:
        status_code = 409
    return JSONResponse(status_code=status_code, content={"detail": exc.to_dict()})


@router.post("/plan", response_model=PlanModel)
def plan_export(body: PlanRequest) -> dict[str, Any] | JSONResponse:
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
def apply_export(body: ApplyRequest) -> dict[str, Any] | JSONResponse:
    require_writeback_enabled("http.usb-export.apply")
    try:
        plan = workflow.ExportPlan.from_dict(body.plan.model_dump())
        receipt = workflow.apply_export(plan, confirmation=body.confirmation)
    except workflow.UsbExportError as exc:
        return _error(exc)
    return receipt.to_dict()


@router.post("/readback", response_model=ReadbackModel)
def readback_export(body: ReadbackRequest) -> dict[str, Any] | JSONResponse:
    try:
        plan = workflow.ExportPlan.from_dict(body.plan.model_dump())
        receipt = workflow.ApplyReceipt.from_dict(body.receipt.model_dump())
        report = workflow.readback_export(plan, receipt)
    except workflow.UsbExportError as exc:
        return _error(exc)
    return report.to_dict()
