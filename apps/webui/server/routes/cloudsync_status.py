"""HTTP status for CloudSync, backed by the common sync-hub status object."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from apps.sync_hub import status as sync_status

router = APIRouter(prefix="/cloudsync", tags=["cloudsync"])


class LastResultOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "error"]
    message: str


class RecentResultOut(LastResultOut):
    finished_at: str
    pushed: int
    pulled: int


class CloudSyncStatusOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool
    reason: str | None
    signed_in_as: str | None
    last_push_at: str | None
    last_pull_at: str | None
    last_result: LastResultOut | None
    rows_pending: int | None
    endpoint: str | None
    recent_results: list[RecentResultOut]


def _data_dir(request: Request) -> Path:
    db_path = Path(str(request.app.state.state_db_path))
    return db_path.resolve().parent.parent


@router.get("/status", response_model=CloudSyncStatusOut)
def get_status(request: Request) -> CloudSyncStatusOut:
    try:
        return CloudSyncStatusOut(**sync_status.read_status(_data_dir(request)).to_wire())
    except sync_status.CloudSyncStatusError as exc:
        raise HTTPException(status_code=500, detail={
            "code": "CLOUDSYNC_STATUS_UNREADABLE", "message": str(exc),
        }) from exc


__all__ = ["CloudSyncStatusOut", "router"]
