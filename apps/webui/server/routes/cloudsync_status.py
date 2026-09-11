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

    #: Three verdicts, not two (round 5 gate T8). ``inconclusive`` is a sync
    #: that completed while its digest compare EXCLUDED rows on one side, so
    #: agreement was never verified. See
    #: :data:`apps.sync_hub.status.ResultStatus`.
    status: Literal["ok", "error", "inconclusive"]
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


def data_dir_for_request(request: Request) -> Path:
    """The data dir (``<data-dir>/state/state.db``) this daemon's CloudSync
    journal and operator actions live in. Shared with ``cloudsync_ops`` so a
    sync it runs journals exactly where ``/status`` reads."""
    db_path = Path(str(request.app.state.state_db_path))
    return db_path.resolve().parent.parent


@router.get("/status", response_model=CloudSyncStatusOut)
def get_status(request: Request) -> CloudSyncStatusOut:
    try:
        return CloudSyncStatusOut(
            **sync_status.read_status(data_dir_for_request(request)).to_wire()
        )
    except sync_status.CloudSyncStatusError as exc:
        raise HTTPException(status_code=500, detail={
            "code": "CLOUDSYNC_STATUS_UNREADABLE", "message": str(exc),
        }) from exc


__all__ = ["CloudSyncStatusOut", "router"]
