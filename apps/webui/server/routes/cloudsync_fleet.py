"""HTTP twins of the hub-local fleet verbs: adopt, revoke, credentials (plan X5).

    GET  /api/v1/cloudsync/fleet/credentials   ENFORCE readiness readout
    POST /api/v1/cloudsync/fleet/adopt          claim a machine for YOURSELF
    POST /api/v1/cloudsync/fleet/revoke         revoke a machine YOU own

The agent-native parity twin of ``python -m apps.sync_hub adopt|revoke|
credentials``: every handler calls the same :mod:`apps.sync_hub.fleet_admin`
function the CLI calls. Two differences, both deliberate and both about who
the caller is:

* The CLI is hub-local and trusts its operator, so ``adopt --owner EMAIL``
  names any signed-in user and ``revoke`` revokes any machine. Over HTTP the
  caller is whoever holds a session cookie, so adopt always adopts to the
  SIGNED-IN user and revoke refuses a machine they do not own. ADR 12 says
  adopt "requires an authenticated session on the hub"; this is that.
* Every route answers 409 ``CLOUDSYNC_NOT_A_HUB`` unless ``MDT_IS_HUB=1``: a
  spoke's own ``machine_owners`` table is not a fleet, and adopting on it
  would write an owner row no hub ever reads.

Not on the ``/api/v1/sync`` router, which authenticates machines by their
sync credential: these authenticate PEOPLE, by their webui session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from apps.shared.state import machine_identity
from apps.sync_hub import enrollment, fleet_admin, machine_credentials
from apps.webui.server.auth import SessionUser
from apps.webui.server.routes.auth import signed_in_user

router = APIRouter(prefix="/cloudsync/fleet", tags=["cloudsync"])


class FleetErrorBody(BaseModel):
    code: str
    message: str


class FleetErrorResponse(BaseModel):
    detail: FleetErrorBody


def _error(description: str) -> dict[str, object]:
    return {"model": FleetErrorResponse, "description": description}


# Exactly what each route can answer, so the contract promises no status a
# route never sends (adopt has no 403; revoke has no 404, since a machine
# with no owner row is its 409 CLOUDSYNC_FLEET).
_NOT_A_HUB: str = "This install is not a hub (CLOUDSYNC_NOT_A_HUB)"
_CREDENTIALS_RESPONSES: dict[int | str, dict[str, object]] = {
    401: _error("Not signed in. code: AUTH_REQUIRED."),
    409: _error(f"{_NOT_A_HUB}."),
}
_ADOPT_RESPONSES: dict[int | str, dict[str, object]] = {
    401: _error("adopt needs a signed-in user. code: AUTH_REQUIRED."),
    404: _error("No such machine on this hub. code: CLOUDSYNC_UNKNOWN_MACHINE."),
    409: _error(
        f"{_NOT_A_HUB}, or the machine is owned by someone else "
        "(CLOUDSYNC_OWNER_CONFLICT) or revoked (CLOUDSYNC_REVOKED)."
    ),
}
_REVOKE_RESPONSES: dict[int | str, dict[str, object]] = {
    401: _error("revoke needs a signed-in user. code: AUTH_REQUIRED."),
    403: _error("The machine is owned by another user. code: CLOUDSYNC_NOT_YOUR_MACHINE."),
    409: _error(f"{_NOT_A_HUB}, or the machine has no owner row to revoke (CLOUDSYNC_FLEET)."),
}


class MachineIdIn(BaseModel):
    machine_id: str = Field(min_length=1)


class AdoptOut(BaseModel):
    machine_id: str
    name: str
    owner_email: str
    enrolled_at: str
    enrolled_via: str
    created: bool


class RevokeOut(BaseModel):
    machine_id: str
    revoked_at: str
    changed: bool
    credential_deleted: bool


class BlockerOut(BaseModel):
    machine_id: str
    name: str
    reason: machine_credentials.BlockerReason


class CredentialMachineOut(BaseModel):
    machine_id: str
    name: str
    ownership: Literal["owned", "unowned", "foreign", "revoked"]
    credential_minted_at: str | None
    is_this_hub: bool


class CredentialsOut(BaseModel):
    hub_machine_id: str
    mode: machine_credentials.CredentialMode
    enforce_ready: bool
    blockers: list[BlockerOut]
    machines: list[CredentialMachineOut]


def _raise(status_code: int, code: str, exc: Exception) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": str(exc)})


def _hub_data_dir(request: Request) -> Path:
    """This hub's data dir, resolved exactly as ``apps.sync_hub.service`` does."""
    if not machine_identity.is_hub_from_env():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "CLOUDSYNC_NOT_A_HUB",
                "message": (
                    f"this install is not a sync hub ({machine_identity.IS_HUB_ENV} "
                    f"is not 1), so it has no fleet to administer."
                ),
            },
        )
    configured = getattr(request.app.state, "sync_hub_data_dir", None)
    if configured is not None:
        return Path(str(configured))
    return Path(str(request.app.state.state_db_path)).resolve().parent.parent


def _require_user(request: Request) -> SessionUser:
    user = signed_in_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "AUTH_REQUIRED",
                "message": "fleet administration needs a signed-in user on this hub.",
            },
        )
    return user


@router.get("/credentials", response_model=CredentialsOut, responses=_CREDENTIALS_RESPONSES)
def read_credentials(request: Request) -> CredentialsOut:
    """Twin of ``python -m apps.sync_hub credentials --json``."""
    _require_user(request)
    return CredentialsOut.model_validate(fleet_admin.credentials(_hub_data_dir(request)))


@router.post("/adopt", response_model=AdoptOut, responses=_ADOPT_RESPONSES)
def adopt_machine(request: Request, body: MachineIdIn) -> AdoptOut:
    """Twin of ``adopt``, always adopting to the signed-in user."""
    user = _require_user(request)
    owner = enrollment.OwnerIdentity(google_sub=user.google_sub, email=user.email)
    try:
        outcome = fleet_admin.adopt(_hub_data_dir(request), machine_id=body.machine_id, owner=owner)
    except fleet_admin.UnknownMachineError as exc:
        raise _raise(404, "CLOUDSYNC_UNKNOWN_MACHINE", exc) from exc
    except enrollment.OwnershipConflictError as exc:
        raise _raise(409, "CLOUDSYNC_OWNER_CONFLICT", exc) from exc
    except enrollment.MachineRevokedError as exc:
        raise _raise(409, "CLOUDSYNC_REVOKED", exc) from exc
    return AdoptOut(**outcome.to_wire())


@router.post("/revoke", response_model=RevokeOut, responses=_REVOKE_RESPONSES)
def revoke_machine(request: Request, body: MachineIdIn) -> RevokeOut:
    """Twin of ``revoke``, limited to machines the signed-in user owns."""
    user = _require_user(request)
    try:
        outcome = fleet_admin.revoke(
            _hub_data_dir(request), machine_id=body.machine_id, require_owner_sub=user.google_sub
        )
    except enrollment.OwnershipConflictError as exc:
        raise _raise(403, "CLOUDSYNC_NOT_YOUR_MACHINE", exc) from exc
    except enrollment.EnrollmentError as exc:
        raise _raise(409, "CLOUDSYNC_FLEET", exc) from exc
    return RevokeOut(**outcome.to_wire())


__all__ = ["router"]
