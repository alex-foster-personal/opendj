"""Feedback pin CloudSync: trigger a sync and read where every pin stands
(FBSYNC-04, agent-native parity; ADR-0013).

Endpoints (all under /api/v1/feedback):

    POST /feedback/sync            reconcile -> one CloudSync round trip -> reconcile
    GET  /feedback/sync/status     store counts, CloudSync link, every pin's state
                                   (``?pin_id=`` narrows it to one pin)

CLI twin: ``python -m apps.sync_hub feedback-pins sync|status --engine URL``.
The engine's CloudSync scheduler (``apps/webui/server/cloudsync_scheduler.py``)
calls :func:`sync_feedback_pins` too, so the button, the agent and the timer
run one code path.

Offline-first: the reconcile before the round trip runs whatever the hub's
state, so the local store is never blocked on CloudSync. An unreachable hub is
never swallowed: it is journaled as an ``error`` result (the same journal
``GET /cloudsync/status`` and the CloudSync status chip read), logged, and
answered here as HTTP 503 ``FEEDBACK_SYNC_HUB_UNREACHABLE``. The next
successful sync offers everything the fence has not delivered, so it catches
up with no bookkeeping of its own.
"""
from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from apps.shared.paths import DATA_DIR
from apps.shared.state import db as state_db
from apps.sync_hub import client as sync_client
from apps.sync_hub import config as sync_config
from apps.sync_hub import maintenance as sync_maintenance
from apps.sync_hub import status as sync_status

from .feedback_replica import PinSyncStatus, pin_statuses, reconcile

router = APIRouter(prefix="/feedback", tags=["feedback"])

# One sync at a time per process: the button, an agent and the scheduler all
# arrive here, and two overlapping round trips against one state DB would
# race each other's push fence.
_SYNC_LOCK = threading.Lock()


# ----- models -------------------------------------------------------------
class FeedbackSyncOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "inconclusive"]
    exported: int
    imported: int
    pushed: int
    pulled: int
    message: str


class PinSyncOut(BaseModel):
    """Where one pin stands. ``attachment_bytes: missing`` is FBSYNC-05's gap:
    the screenshot's metadata synced here but its bytes did not."""

    model_config = ConfigDict(frozen=True)

    pin_id: str
    state: Literal["synced", "pending_push", "unreconciled", "harvested"]
    archived: bool
    updated_at: str
    origin_device_id: str | None
    attachment_bytes: Literal["none", "present", "missing"]


class FeedbackStoreSyncOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    live: int
    archived: int
    synced: int
    pending_push: int
    unreconciled: int
    harvested: int
    attachments_missing: int


SchedulerState = Literal["not_armed", "off", "misconfigured", "running", "stopped", "dead"]


class CloudSyncSchedulerOut(BaseModel):
    """The engine's CloudSync scheduler: is its thread alive, and what last failed.

    ``not_armed``: this app never builds one (tests, non-daemon boots).
    ``off``: armed, switch not on. ``misconfigured``: switched on but it could
    not start (``reason`` says why); the engine booted regardless.
    ``dead``: it was started and its thread is gone without ``stop()``.
    """

    model_config = ConfigDict(frozen=True)

    state: SchedulerState
    alive: bool
    reason: str | None
    ticks: int
    last_ok_at: str | None
    last_error: str | None
    last_error_at: str | None


NOT_ARMED = CloudSyncSchedulerOut(
    state="not_armed", alive=False, reason="this engine does not arm the CloudSync scheduler",
    ticks=0, last_ok_at=None, last_error=None, last_error_at=None,
)


class CloudSyncLinkOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool
    reason: str | None
    endpoint: str | None
    last_result: str | None
    last_message: str | None
    last_finished_at: str | None


class FeedbackSyncStatusOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    store: FeedbackStoreSyncOut
    cloudsync: CloudSyncLinkOut
    scheduler: CloudSyncSchedulerOut
    pins: list[PinSyncOut]


# ----- the engine's two stores --------------------------------------------
class FeedbackSyncError(RuntimeError):
    """A sync that did not complete, carrying the HTTP answer it maps to."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code

    def to_http(self) -> HTTPException:
        return HTTPException(
            status_code=self.status_code, detail={"code": self.code, "message": str(self)}
        )


@dataclass(frozen=True)
class FeedbackStore:
    """The pin store and the state DB one engine owns. They must share a data dir."""

    feedback_root: Path
    data_dir: Path
    state_db_path: Path
    #: The CloudSync machine name: the one saved in this data dir's
    #: ``cloudsync-config.json`` (what the scheduler and Sync now register
    #: under), and only when none is saved the engine's own published hostname
    #: (``MUSIC_DJ_HOSTNAME`` else the OS hostname). ``machines.name`` is
    #: UNIQUE: two data dirs on one host need two names, and the hostname may
    #: already belong to another machine id in a pulled ``machines`` table.
    machine_name: str


def store_for_app(app: FastAPI) -> FeedbackStore:
    """Resolve both stores from the app, refusing a split layout.

    ``feedback.py`` writes pins under ``app.state.data_dir`` and CloudSync
    reads the state DB at ``app.state.state_db_path``. If those name two
    different data dirs, a sync would push one machine's pins from another
    machine's database, so this fails instead of guessing.
    """
    state_db_path = Path(str(app.state.state_db_path)).resolve()
    data_dir = state_db_path.parent.parent
    configured = getattr(app.state, "data_dir", None)
    feedback_data_dir = Path(configured).resolve() if configured is not None else DATA_DIR.resolve()
    if state_db_path.parent.name != "state" or feedback_data_dir != data_dir:
        raise FeedbackSyncError(
            500,
            "FEEDBACK_SYNC_LAYOUT",
            f"feedback pins live under {feedback_data_dir} but the state DB is "
            f"{state_db_path}; both must share one data dir "
            f"(<data-dir>/feedback and <data-dir>/state/state.db)",
        )
    try:
        configured_name = sync_config.configured_machine_name(data_dir)
    except sync_config.CloudSyncConfigError as exc:
        raise FeedbackSyncError(500, "CLOUDSYNC_CONFIG_INVALID", str(exc)) from exc
    return FeedbackStore(
        feedback_root=feedback_data_dir / "feedback",
        data_dir=data_dir,
        state_db_path=state_db_path,
        machine_name=configured_name if configured_name is not None else str(app.state.hostname),
    )


def _hub_url(store: FeedbackStore) -> str:
    link = sync_status.read_status(store.data_dir)
    if link.endpoint is None:
        raise FeedbackSyncError(
            409,
            "CLOUDSYNC_NOT_CONFIGURED",
            f"no CloudSync hub is configured ({link.reason}); set "
            f"{sync_status.ENDPOINT_ENV}. Pins keep working locally meanwhile.",
        )
    return link.endpoint


def _reconcile(store: FeedbackStore) -> tuple[int, int]:
    conn = state_db.open_rw(store.state_db_path)
    try:
        result = reconcile(conn, store.feedback_root)
    finally:
        conn.close()
    return result.exported, result.imported


def sync_feedback_pins(store: FeedbackStore) -> FeedbackSyncOut:
    """Reconcile, run one CloudSync round trip, reconcile again.

    Raises :class:`FeedbackSyncError`; every failure has already been
    journaled by :func:`apps.sync_hub.maintenance.sync` before it propagates.
    """
    if not _SYNC_LOCK.acquire(blocking=False):
        raise FeedbackSyncError(409, "FEEDBACK_SYNC_IN_PROGRESS", "a sync is already running")
    try:
        hub_url = _hub_url(store)
        exported, _ = _reconcile(store)
        try:
            result = sync_maintenance.sync(store.data_dir, hub_url, name=store.machine_name)
        except sync_client.SyncTransportError as exc:
            raise FeedbackSyncError(
                503,
                "FEEDBACK_SYNC_HUB_UNREACHABLE",
                f"CloudSync hub {hub_url} is unreachable: {exc}. Local pins are "
                f"unaffected; {exported} newly exported pin version(s) and every "
                f"earlier undelivered one wait for the next sync.",
            ) from exc
        except (
            sync_client.SyncApplyError,
            sync_client.SyncDigestMismatch,
            sync_client.SyncProtocolError,
            sync_client.SyncStillMoving,
            sync_client.SyncVersionMismatch,
        ) as exc:
            raise FeedbackSyncError(
                502, "FEEDBACK_SYNC_FAILED", f"{type(exc).__name__}: {exc}"
            ) from exc
        _, imported = _reconcile(store)
    finally:
        _SYNC_LOCK.release()
    return FeedbackSyncOut(
        status="inconclusive" if result.digest_inconclusive else "ok",
        exported=exported,
        imported=imported,
        pushed=result.pushed,
        pulled=result.pulled,
        message=(
            f"synced against {hub_url}: pushed {result.pushed}, pulled "
            f"{result.pulled}, {imported} pin version(s) written to this store"
        ),
    )


def _pin_out(status: PinSyncStatus) -> PinSyncOut:
    return PinSyncOut(
        pin_id=status.pin_id,
        state=status.state,
        archived=status.archived,
        updated_at=status.updated_at,
        origin_device_id=status.origin_device_id,
        attachment_bytes=status.attachment_bytes,
    )


def _store_counts(statuses: list[PinSyncStatus]) -> FeedbackStoreSyncOut:
    states = Counter(status.state for status in statuses)
    archived = sum(status.archived for status in statuses)
    return FeedbackStoreSyncOut(
        live=len(statuses) - archived,
        archived=archived,
        synced=states["synced"],
        pending_push=states["pending_push"],
        unreconciled=states["unreconciled"],
        harvested=states["harvested"],
        attachments_missing=sum(status.attachment_bytes == "missing" for status in statuses),
    )


def _link_out(link: sync_status.CloudSyncStatus) -> CloudSyncLinkOut:
    latest = link.recent_results[0] if link.recent_results else None
    return CloudSyncLinkOut(
        enabled=link.enabled,
        reason=link.reason,
        endpoint=link.endpoint,
        last_result=None if latest is None else latest.status,
        last_message=None if latest is None else latest.message,
        last_finished_at=None if latest is None else latest.finished_at,
    )


def _only_pin(statuses: list[PinSyncStatus], pin_id: str) -> list[PinSyncStatus]:
    """The one pin asked for, or a 404: an unknown id is never an empty list."""
    selected = [status for status in statuses if status.pin_id == pin_id]
    if not selected:
        raise FeedbackSyncError(
            404, "COMMENT_NOT_FOUND", f"no pin {pin_id!r} on this machine or in its sync table"
        )
    return selected


def scheduler_status(app: FastAPI) -> CloudSyncSchedulerOut:
    """The scheduler the lifespan retained on the app, or ``not_armed``."""
    scheduler = getattr(app.state, "cloudsync_scheduler", None)
    return NOT_ARMED if scheduler is None else scheduler.status()


def feedback_sync_status(
    store: FeedbackStore, scheduler: CloudSyncSchedulerOut, pin_id: str | None = None
) -> FeedbackSyncStatusOut:
    conn = state_db.open_rw(store.state_db_path)
    try:
        statuses = pin_statuses(conn, store.feedback_root)
    finally:
        conn.close()
    if pin_id is not None:
        statuses = _only_pin(statuses, pin_id)
    return FeedbackSyncStatusOut(
        store=_store_counts(statuses),
        cloudsync=_link_out(sync_status.read_status(store.data_dir)),
        scheduler=scheduler,
        pins=[_pin_out(status) for status in statuses],
    )


# ----- routes -------------------------------------------------------------
@router.post("/sync", response_model=FeedbackSyncOut)
def post_feedback_sync(request: Request) -> FeedbackSyncOut:
    try:
        return sync_feedback_pins(store_for_app(request.app))
    except FeedbackSyncError as exc:
        raise exc.to_http() from exc


@router.get("/sync/status", response_model=FeedbackSyncStatusOut)
def get_feedback_sync_status(
    request: Request, pin_id: str | None = Query(None)
) -> FeedbackSyncStatusOut:
    try:
        return feedback_sync_status(
            store_for_app(request.app), scheduler_status(request.app), pin_id
        )
    except FeedbackSyncError as exc:
        raise exc.to_http() from exc


__all__ = [
    "NOT_ARMED",
    "CloudSyncSchedulerOut",
    "FeedbackStore",
    "FeedbackSyncError",
    "FeedbackSyncOut",
    "FeedbackSyncStatusOut",
    "feedback_sync_status",
    "router",
    "store_for_app",
    "sync_feedback_pins",
]
