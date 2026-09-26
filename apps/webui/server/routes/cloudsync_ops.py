"""HTTP twins of the CloudSync operator CLI (plan W4, agent-native parity).

    POST /api/v1/cloudsync/sync               -- ``python -m apps.sync_hub sync``
    GET  /api/v1/cloudsync/fleet              -- ``python -m apps.sync_hub fleet --json``
    POST /api/v1/cloudsync/enrollment-grants  -- ``python -m apps.sync_hub grant``

Each route calls the SAME function its CLI verb calls
(:func:`apps.sync_hub.maintenance.sync`, :func:`apps.sync_hub.maintenance_enroll.fleet`,
:func:`apps.sync_hub.maintenance_enroll.grant`), so the two entry points cannot
drift: there is no second implementation here to drift. ``sync`` journals
through ``maintenance.sync`` itself, which is why ``GET /cloudsync/status``
shows an HTTP-triggered run exactly as it shows a CLI one.

Local operator only. These are acts of machine authority, and the CLI twin
needs a shell on the machine. A hub listens on its tailnet address so spokes
can reach ``/api/v1/sync/*``; without a guard, any tailnet peer could point a
spoke's ``/cloudsync/sync`` at a hub it controls (pushing the library there)
or mint a grant for any signed-in owner. So every route here refuses any
caller :func:`apps.webui.server.local_operator.local_operator_refusal` does
not accept: a non-loopback peer, a request relayed by a proxy (cloudflared,
Tailscale Serve), a non-loopback ``Host`` (DNS rebinding, the share host), a
non-loopback ``Origin`` and a ``cross-site`` fetch.

``hub_url`` is a required body field for now. A parallel config item will add
a stored default; the field stays so an explicit URL always wins.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.machine_pressure import read_machine_pressure
from apps.shared.state.machine_identity import MachineIdentityError, is_hub_from_env
from apps.shared.sync_runtime_gates import (
    DEFER_REASON_SYNC_IN_PROGRESS,
    SyncDeferredError,
    refuse_sync_round,
)
from apps.sync_hub import client as sync_client
from apps.sync_hub import enrollment_credentials, maintenance, maintenance_enroll
from apps.sync_hub.scheduler_owed import mark_scheduler_owed
from apps.sync_hub.single_flight import sync_lock_for
from apps.webui.server.local_operator import local_operator_refusal

from .cloudsync_status import data_dir_for_request

router = APIRouter(prefix="/cloudsync", tags=["cloudsync"])

logger = logging.getLogger(__name__)

#: The sync failures ``client.run_sync`` DECLARES (see its docstring) that are
#: not a transport failure. Anything else propagates as a 500, still journaled
#: as ``error`` by ``maintenance.sync``.
DECLARED_SYNC_REFUSALS: tuple[type[Exception], ...] = (
    sync_client.SyncDigestMismatch,
    sync_client.SyncStillMoving,
    sync_client.SyncVersionMismatch,
    sync_client.SyncApplyError,
    sync_client.SyncProtocolError,
)

#: One sync at a time per data dir, shared with the scheduler's rounds
#: (:mod:`apps.sync_hub.single_flight`). ``run_sync`` is not re-entrant
#: against one state DB, and neither a double-clicked button nor a Sync now
#: during a scheduler round may start a second one.


# ----- models --------------------------------------------------------------


class OpsErrorBody(BaseModel):
    code: str
    message: str


class OpsErrorResponse(BaseModel):
    """FastAPI wraps an ``HTTPException`` detail under ``detail``."""

    detail: OpsErrorBody


class SyncRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hub_url: str = Field(
        min_length=1,
        pattern=r"^https?://",
        description="the hub base URL, e.g. http://hub.tailnet:8686 (CLI --hub)",
    )
    name: str | None = Field(
        default=None,
        min_length=1,
        description="this machine's display name; the hostname when omitted (CLI --name)",
    )
    force: bool = Field(
        default=False,
        description="Bypass Gig posture and playing-deck gates for this round only.",
    )


class SyncRunOut(BaseModel):
    """What one ``run_sync`` observed. Every count is observed, none inferred.

    ``digest_inconclusive`` True is the CLI's exit code 4: rows moved but the
    post-sync digest compare excluded rows, so agreement was NOT verified.
    ``hub_quarantined`` None means the hub did not report it (unknown, not 0).
    """

    model_config = ConfigDict(frozen=True)

    machine_id: str
    hub_machine_id: str
    pushed: int
    accepted: int
    rejected: int
    pulled: int
    applied: int
    hub_seq: int
    rounds: int
    hub_restore_detected: bool
    quarantined_rows: int
    quarantined_incoming: int
    hub_quarantined: int | None
    digest_inconclusive: bool


class FleetMachineOut(BaseModel):
    """One row of ``fleet --json``. ``extra=forbid`` so a field the shared
    function adds fails loudly here instead of silently vanishing from HTTP."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    machine_id: str
    name: str
    state: Literal["owned", "unowned", "foreign"]
    owner_google_sub: str | None
    owner_email: str | None
    row_hub_machine_id: str | None
    enrolled_at: str | None
    enrolled_via: str | None


class FleetOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hub_machine_id: str
    machines: list[FleetMachineOut]
    owned: int
    unowned: int
    foreign: int


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_email: str = Field(
        min_length=1,
        description="email of a user who has signed in on this hub (CLI --owner)",
    )
    ttl_seconds: int = Field(
        default=enrollment_credentials.GRANT_TTL_S,
        gt=0,
        le=enrollment_credentials.GRANT_TTL_MAX_S,
        description="how long the grant stays redeemable (CLI --ttl-seconds)",
    )


class SchedulerResumeOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool


class GrantOut(BaseModel):
    """The raw token exists in this response and nowhere else; only its
    sha256 is stored. Redeem with ``python -m apps.sync_hub enroll``."""

    model_config = ConfigDict(frozen=True)

    token: str
    owner_email: str
    created_at: str
    expires_at: str


def _declared(description: str) -> dict[str, Any]:
    return {"model": OpsErrorResponse, "description": description}


LOCAL_ONLY_RESPONSE: dict[int | str, dict[str, Any]] = {
    403: _declared(
        "The caller is not the local operator: a non-loopback peer, a "
        "proxy-relayed request, a non-loopback Host or Origin, or a "
        "cross-site fetch. code: CLOUDSYNC_OPS_LOCAL_ONLY."
    ),
}

SYNC_RESPONSES: dict[int | str, dict[str, Any]] = {
    **LOCAL_ONLY_RESPONSE,
    409: _declared(
        "CLOUDSYNC_SYNC_IN_PROGRESS: a sync is already running in this "
        "process; refused before syncing, so NOT journaled (force=true does "
        "not bypass this). "
        "CLOUDSYNC_SYNC_DEFERRED: Gig posture or a playing deck blocked sync "
        "before any hub I/O when force=false; NOT journaled. "
        "CLOUDSYNC_SYNC_REFUSED: the sync raised one of run_sync's declared "
        "refusals (digest mismatch, still moving, schema version mismatch, "
        "apply or protocol error); journaled as error."
    ),
    500: {
        "description": (
            "run_sync raised an error it does not declare (a defect). "
            "Journaled as error by maintenance.sync; the body is the "
            "server's plain 500, not an OpsErrorResponse."
        ),
    },
    502: _declared(
        "The hub was unreachable or answered with something unusable. code: "
        "CLOUDSYNC_HUB_UNREACHABLE. Journaled as error."
    ),
}

GRANT_RESPONSES: dict[int | str, dict[str, Any]] = {
    **LOCAL_ONLY_RESPONSE,
    404: _declared(
        "No signed-in user on this hub has that email; none is created. "
        "code: CLOUDSYNC_GRANT_OWNER_UNKNOWN."
    ),
    409: _declared(
        "This machine is not a hub (MDT_IS_HUB is not 1); a grant is an act "
        "of hub authority. code: CLOUDSYNC_NOT_A_HUB."
    ),
    500: _declared(
        "MDT_IS_HUB holds a value other than 1, 0 or empty. code: CLOUDSYNC_HUB_FLAG_INVALID."
    ),
}


# ----- helpers -------------------------------------------------------------


def _refuse(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def require_local_operator(request: Request) -> None:
    """403 unless the caller is the machine's own operator. See module doc."""
    reason = local_operator_refusal(request)
    if reason is not None:
        raise _refuse(
            403,
            "CLOUDSYNC_OPS_LOCAL_ONLY",
            f"CloudSync operator actions are local-operator only: {reason}. "
            "Run them on the machine itself.",
        )


@contextmanager
def _one_sync_at_a_time(data_dir: Path) -> Iterator[None]:
    lock = sync_lock_for(data_dir)
    if not lock.acquire(blocking=False):
        raise _refuse(
            409,
            "CLOUDSYNC_SYNC_IN_PROGRESS",
            "a CloudSync sync (a Sync now or a scheduler round) is already "
            "running against this data dir; read GET /api/v1/cloudsync/status "
            "for its outcome.",
        )
    try:
        yield
    finally:
        lock.release()


def _require_hub() -> None:
    try:
        is_hub = is_hub_from_env()
    except MachineIdentityError as exc:
        raise _refuse(500, "CLOUDSYNC_HUB_FLAG_INVALID", str(exc)) from exc
    if not is_hub:
        raise _refuse(
            409,
            "CLOUDSYNC_NOT_A_HUB",
            "this machine is not a hub (MDT_IS_HUB is not 1). Mint "
            "the grant on the hub the joining machine will enroll with.",
        )


def _sync_out(result: sync_client.SyncResult) -> SyncRunOut:
    return SyncRunOut(
        machine_id=result.machine_id,
        hub_machine_id=result.hub_machine_id,
        pushed=result.pushed,
        accepted=result.accepted,
        rejected=result.rejected,
        pulled=result.pulled,
        applied=result.applied,
        hub_seq=result.hub_seq,
        rounds=result.rounds,
        hub_restore_detected=result.hub_restore_detected,
        quarantined_rows=result.quarantined_rows,
        quarantined_incoming=result.quarantined_incoming,
        hub_quarantined=result.hub_quarantined,
        digest_inconclusive=result.digest_inconclusive,
    )


# ----- routes --------------------------------------------------------------


@router.post(
    "/scheduler/resume-owed",
    response_model=SchedulerResumeOut,
    responses=LOCAL_ONLY_RESPONSE,
    dependencies=[Depends(require_local_operator)],
)
def resume_scheduler_owed(request: Request) -> SchedulerResumeOut:
    """Mark a deferred scheduler round owed and wake the in-process scheduler."""
    data_dir = data_dir_for_request(request)
    mark_scheduler_owed(data_dir)
    sched = getattr(request.app.state, "sync_hub_scheduler", None)
    if sched is not None:
        sched._next_due = time.monotonic()
    return SchedulerResumeOut(ok=True)


def _pressure_for_request(request: Request) -> dict[str, Any]:
    """Live sampler by default; tests may pin a calm payload on ``app.state``."""
    override = getattr(request.app.state, "machine_pressure", None)
    if isinstance(override, dict):
        return override
    payload = read_machine_pressure()
    return dict(payload) if isinstance(payload, dict) else {}


@router.post(
    "/sync",
    response_model=SyncRunOut,
    responses=SYNC_RESPONSES,
    dependencies=[Depends(require_local_operator)],
)
def run_sync_round(body: SyncRunIn, request: Request) -> SyncRunOut:
    """One spoke round trip against ``hub_url``, journaled for ``/status``."""
    data_dir: Path = data_dir_for_request(request)
    mirror = getattr(request.app.state, "ui_mirror", None)
    if mirror is not None and not isinstance(mirror, dict):
        mirror = None
    reason = refuse_sync_round(
        data_dir,
        mirror,
        force=body.force,
        pressure_payload=_pressure_for_request(request),
    )
    if reason is not None:
        raise _refuse(409, "CLOUDSYNC_SYNC_DEFERRED", f"CloudSync sync deferred: {reason}")
    with _one_sync_at_a_time(data_dir):

        try:
            result = maintenance.sync(
                data_dir,
                body.hub_url,
                name=body.name,
                ui_mirror=mirror,
                force=body.force,
            )
        except sync_client.SyncTransportError as exc:
            raise _refuse(502, "CLOUDSYNC_HUB_UNREACHABLE", str(exc)) from exc
        except DECLARED_SYNC_REFUSALS as exc:
            raise _refuse(409, "CLOUDSYNC_SYNC_REFUSED", f"{type(exc).__name__}: {exc}") from exc
        except SyncDeferredError as exc:
            # ``maintenance.sync`` re-checks the Gig/deck gate itself and now
            # also wraps ``client.run_sync`` in a cross-process flock
            # (Codex review, PR #3831, P2/NON-BLOCKING): a standalone CLI
            # holding that flock at the exact moment this route calls in
            # raises this uncaught before, turning an expected busy-skip
            # into a 500. Both reasons were already refused before any hub
            # I/O, so both get a documented 409 here, same as the pre-check
            # above: sync_in_progress reuses _one_sync_at_a_time's own code
            # so a caller sees ONE code for "already running" regardless of
            # whether the other holder is in this process or a standalone
            # CLI in another one; anything else (a Gig/deck reason lost to
            # the same race the pre-check above already covers on every
            # OTHER timing) falls back to the pre-check's own code.
            code = (
                "CLOUDSYNC_SYNC_IN_PROGRESS"
                if exc.reason == DEFER_REASON_SYNC_IN_PROGRESS
                else "CLOUDSYNC_SYNC_DEFERRED"
            )
            raise _refuse(409, code, f"CloudSync sync deferred: {exc.reason}") from exc
    return _sync_out(result)


@router.get(
    "/fleet",
    response_model=FleetOut,
    responses=LOCAL_ONLY_RESPONSE,
    dependencies=[Depends(require_local_operator)],
)
def get_fleet(request: Request) -> dict[str, Any]:
    """Exactly ``fleet --json``: the one function both entry points call."""
    return maintenance_enroll.fleet(data_dir_for_request(request))


@router.post(
    "/enrollment-grants",
    response_model=GrantOut,
    status_code=201,
    responses=GRANT_RESPONSES,
    dependencies=[Depends(require_local_operator)],
)
def mint_enrollment_grant(body: GrantIn, request: Request, response: Response) -> GrantOut:
    """Mint one single-use grant on this hub. The token is returned once and
    never logged; only its sha256 reaches the database."""
    _require_hub()
    try:
        minted = maintenance_enroll.grant(
            data_dir_for_request(request),
            owner_email=body.owner_email,
            ttl_s=body.ttl_seconds,
        )
    except enrollment_credentials.EnrollmentCredentialError as exc:
        # GrantIn already bounds ttl_seconds to mint_grant's own range, so the
        # one refusal left is owner_by_email's: no signed-in user by that email.
        raise _refuse(404, "CLOUDSYNC_GRANT_OWNER_UNKNOWN", str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    logger.info("minted enrollment grant for %s, expires %s", minted.owner.email, minted.expires_at)
    return GrantOut(
        token=minted.token,
        owner_email=minted.owner.email,
        created_at=minted.created_at,
        expires_at=minted.expires_at,
    )


__all__ = [
    "FleetOut",
    "GrantIn",
    "GrantOut",
    "SyncRunIn",
    "SyncRunOut",
    "require_local_operator",
    "router",
]
