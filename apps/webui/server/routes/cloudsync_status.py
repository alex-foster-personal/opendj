"""HTTP status for CloudSync, backed by the common sync-hub status object."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.state import db as state_db
from apps.sync_hub import status as sync_status
from apps.sync_hub import sync_set

router = APIRouter(prefix="/cloudsync", tags=["cloudsync"])

ConfigSourceOut = Literal["env", "file", "default"]


class LastResultOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: Three verdicts, not two (round 5 gate T8). ``inconclusive`` is a sync
    #: that completed while its digest compare EXCLUDED rows on one side, so
    #: agreement was never verified. See
    #: :data:`apps.sync_hub.status.ResultStatus`.
    status: Literal["ok", "error", "inconclusive", "deferred"]
    message: str


class RecentResultOut(LastResultOut):
    finished_at: str
    pushed: int
    pulled: int


class UpdateRequiredOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: Literal["SYNC_WIRE_VERSION"]
    local_wire_version: int
    peer_wire_version: int
    action: str


class CloudSyncStatusOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(description="configured AND running: a scheduler heartbeat is fresh")
    configured: bool = Field(description="the effective config is on and names a hub")
    running: bool = Field(description="a scheduler heartbeat is fresh")
    heartbeat_at: str | None = Field(
        description="UTC time of the last scheduler beat, fresh or stale"
    )
    enabled_source: ConfigSourceOut = Field(
        description="which source decided 'enabled': env override, config file, or default"
    )
    endpoint_source: ConfigSourceOut = Field(
        description="which source decided the hub URL: env override, config file, or default"
    )
    reason: str | None
    signed_in_as: str | None
    last_push_at: str | None
    last_pull_at: str | None
    last_result: LastResultOut | None
    rows_pending: int | None
    hash_pending: int | None = Field(
        default=None,
        description="live tracks offered as hash_pending on the hub (ADR-0047)",
    )
    quarantined: int | None = Field(
        default=None,
        description="rows held out of the sync set for stamp or identity-dup reasons",
    )
    endpoint: str | None
    recent_results: list[RecentResultOut]
    update_required: UpdateRequiredOut | None = Field(
        default=None,
        description=(
            "Present when the latest sync failed with SYNC_WIRE_VERSION: both wire "
            "versions and the install action for this machine."
        ),
    )


class IdentityBacklogOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    unsyncable_inferred: int = Field(
        description=(
            "live 'tracks' rows still held for content-identity duplicate losers "
            "only. Rows lacking hash and ISRC travel as hash_pending instead."
        )
    )
    hash_pending: int = Field(
        description=(
            "live 'tracks' rows offered to the hub with hash_pending=true while "
            "awaiting content_hash. Run "
            "`python -m apps.shared.state.backfill_content_hash --for-hub --live` "
            "on a machine that holds the audio."
        )
    )


def data_dir_for_request(request: Request) -> Path:
    """The data dir (``<data-dir>/state/state.db``) this daemon's CloudSync
    journal and operator actions live in. Shared with ``cloudsync_ops`` so a
    sync it runs journals exactly where ``/status`` reads."""
    db_path = Path(str(request.app.state.state_db_path))
    return db_path.resolve().parent.parent


cloudsync_data_dir = data_dir_for_request


UNREADABLE_RESPONSES: dict[int | str, dict[str, Any]] = {
    500: {"description": "a CloudSync state file in the data dir is malformed or unreadable"}
}


@router.get("/status", response_model=CloudSyncStatusOut, responses=UNREADABLE_RESPONSES)
def get_status(request: Request) -> CloudSyncStatusOut:
    data_dir = data_dir_for_request(request)
    try:
        wire = sync_status.read_status(data_dir).to_wire()
    except sync_status.CloudSyncStatusError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "CLOUDSYNC_STATUS_UNREADABLE",
                "message": str(exc),
            },
        ) from exc
    db_path = data_dir / "state" / "state.db"
    conn = state_db.open_ro(db_path)
    try:
        excluded = sync_set.excluded_counts(conn)
        wire["hash_pending"] = sync_set.count_hash_pending(conn)
        wire["quarantined"] = sum(excluded.values()) if excluded else 0
    finally:
        conn.close()
    return CloudSyncStatusOut(**wire)


@router.get("/identity-backlog", response_model=IdentityBacklogOut)
def get_identity_backlog(request: Request) -> IdentityBacklogOut:
    """Cheap, read-only count of the identity-hold sync backlog (CLOUDSYNC-16:
    the maintainer's admin panel could say 'inconclusive' forever with no way to tell a
    structural backlog from a transient hiccup). One indexed-ish table scan,
    no file I/O, no digest walk."""
    db_path = data_dir_for_request(request) / "state" / "state.db"
    conn = state_db.open_ro(db_path)
    try:
        dup_only = sync_set.count_unsyncable_inferred(conn)
        pending = sync_set.count_hash_pending(conn)
    finally:
        conn.close()
    return IdentityBacklogOut(unsyncable_inferred=dup_only, hash_pending=pending)


__all__ = [
    "UNREADABLE_RESPONSES",
    "CloudSyncStatusOut",
    "IdentityBacklogOut",
    "UpdateRequiredOut",
    "cloudsync_data_dir",
    "data_dir_for_request",
    "router",
]
