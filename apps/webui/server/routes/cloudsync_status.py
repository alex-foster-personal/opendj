"""HTTP status for CloudSync, backed by the common sync-hub status object."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple

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


class DigestDiffSampleOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    table: str
    stable_id: str
    newer_side: str
    stamp: str = ""


class CredentialNoticeOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    verdict: str
    action: str
    hub_machine_id: str


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
        description="live tracks offered as hash_pending on the hub (ADR-0068)",
    )
    quarantined: int | None = Field(
        default=None,
        description="rows held for direct stamp faults or identity-dup losers only",
    )
    excluded_total: int | None = Field(
        default=None,
        description="every row this machine holds outside the sync set, including transitives",
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
    digest_diff: list[DigestDiffSampleOut] | None = Field(
        default=None,
        description="Sample divergent rows when the latest result is a digest mismatch.",
    )
    credential_notice: CredentialNoticeOut | None = Field(
        default=None,
        description="One-shot re-enroll notice while the hub reads the credential as missing.",
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


# ----- one sync-set count walk at a time (PERF-RB-04) -------------------------


class SyncSetCounts(NamedTuple):
    """The three sync-set readouts ``GET /status`` adds to the status file."""

    hash_pending: int
    quarantined: int
    excluded_total: int


@dataclass
class _Flight:
    """One running count walk and the callers waiting on its answer."""

    done: threading.Event = field(default_factory=threading.Event)
    waiters: int = 0
    result: SyncSetCounts | None = None
    error: BaseException | None = None


_FLIGHTS_LOCK = threading.Lock()
_FLIGHTS: dict[Path, _Flight] = {}


def count_sync_set(db_path: Path) -> SyncSetCounts:
    """Walk the sync set once: every row judged in Python, so it is slow."""
    conn = state_db.open_ro(db_path)
    try:
        return SyncSetCounts(
            hash_pending=sync_set.count_hash_pending(conn),
            quarantined=sync_set.count_quarantined_roots(conn),
            excluded_total=sync_set.excluded_total(conn),
        )
    finally:
        conn.close()


def in_flight_waiters(db_path: Path) -> int:
    """Callers waiting on the walk now running for ``db_path`` (0 when none)."""
    with _FLIGHTS_LOCK:
        flight = _FLIGHTS.get(db_path)
        return 0 if flight is None else flight.waiters


def sync_set_counts(db_path: Path) -> SyncSetCounts:
    """The sync-set counts, from the walk already running when there is one.

    The walk judges every sync-set row in Python, and the status chip polls
    this route every 30 s from each open tab. Before this, every poll started
    its own walk. On Mon 5 Oct 2026 the silver preview held ELEVEN at once,
    each over 120 s: 32% of the backend's sampled CPU, and a GIL convoy that
    cut the All Tracks listing to about 10 rows/s (a cold walk measured 18.9 s
    alone and 97.7 s beside six status pollers). A caller that arrives while
    a walk runs now waits for it and takes its answer, so at most one walk per
    database runs at any time. Nothing is cached: the first call after a walk
    finishes starts a new one, so an answer is never older than the walk the
    caller joined. A failed walk raises in every caller that joined it.
    """
    with _FLIGHTS_LOCK:
        flight = _FLIGHTS.get(db_path)
        leader = flight is None
        if flight is None:
            flight = _FLIGHTS[db_path] = _Flight()
        else:
            flight.waiters += 1
    if not leader:
        flight.done.wait()
        if flight.error is not None:
            raise flight.error
        assert flight.result is not None
        return flight.result
    try:
        flight.result = count_sync_set(db_path)
    except BaseException as exc:
        flight.error = exc
        raise
    finally:
        with _FLIGHTS_LOCK:
            del _FLIGHTS[db_path]
        flight.done.set()
    return flight.result


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
    counts = sync_set_counts(data_dir / "state" / "state.db")
    wire["hash_pending"] = counts.hash_pending
    wire["quarantined"] = counts.quarantined
    wire["excluded_total"] = counts.excluded_total
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
