"""Rekordbox ANALYSE-ME routes (RBXAN-8322c): plan, apply, adopt.

``GET  /api/v1/rekordbox/analyse/plan``   read-only plan plus whether Rekordbox runs
``POST /api/v1/rekordbox/analyse/apply``  guarded write of the confirmed plan
``POST /api/v1/rekordbox/analyse/adopt``  bring Rekordbox's analysis into Open DJ

A guard refusal is 409 with its ``code`` (nothing written); a failed write is
500 and says whether the backup was restored. The gate refusal comes from the
app-wide ``RekordboxWritebackDisabled`` handler.

-Claude
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from apps.rekordbox_analyse import service
from apps.rekordbox_analyse.writer import AnalyseApplyFailed, AnalyseApplyRefused
from apps.shared.rekordbox_writeback import require_writeback_enabled

router = APIRouter(prefix="/rekordbox/analyse", tags=["rekordbox-analyse"])


class ApplyIn(BaseModel):
    plan_sha: str
    cloud_sync_off_confirmed: bool


@router.get("/plan")
def get_plan() -> dict[str, object]:
    try:
        return service.plan_status(service.locations())
    except FileNotFoundError as error:
        raise HTTPException(404, {"code": "rekordbox_not_found", "message": str(error)}) from error
    except ValueError as error:
        raise HTTPException(409, {"code": "rekordbox_layout", "message": str(error)}) from error


@router.post("/apply")
def post_apply(body: ApplyIn) -> dict[str, object]:
    require_writeback_enabled("http.rekordbox.analyse.apply")
    try:
        report = service.apply(
            body.plan_sha, cloud_sync_off_confirmed=body.cloud_sync_off_confirmed, where=service.locations()
        )
    except AnalyseApplyRefused as error:
        raise HTTPException(409, error.to_dict()) from error
    except AnalyseApplyFailed as error:
        raise HTTPException(500, error.to_dict()) from error
    return report.to_dict()


@router.post("/adopt")
def post_adopt() -> dict[str, object]:
    return service.adopt(service.locations())
