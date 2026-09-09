"""The enrollment half of ``POST /api/v1/sync/enroll``: models and mapping.

Split out of :mod:`apps.sync_hub.service` for the quality-gate file_size
ratchet, exactly as ``protocol_common``, ``client_transport_ops`` and
``cloudsync_overview`` were before it. The route itself stays in
``service.py`` with its siblings, because that file owns the request
plumbing -- the connection, the transaction, the schema-version handshake
and the hub's own identity. What lives here is everything downstream of
those: the payload shapes, and the one place a credential failure becomes an
HTTP status.

The status mapping is the part worth keeping together in one readable block:

  401  the credential did not establish an owner
  501  the credential KIND is real and reserved but not built yet
  409  the machine is already owned by somebody else, its owner row is
       revoked, or the enrollment was otherwise refused

The three 409s carry different ``code`` values rather than one, because the
remedies differ: a conflict wants the rightful owner, a revocation wants a
deliberate decision to un-revoke, and the generic case wants the message.

501 rather than 401 for the reserved ``google_id_token`` kind is deliberate.
An unimplemented seam answering like a bad token is indistinguishable from a
typo, and whoever picks up the in-app path would have nothing to find.
"""
from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from apps.sync_hub import enrollment, enrollment_credentials
from apps.sync_hub.protocol import MachineRow


class EnrollCredentialModel(BaseModel):
    """The credential the joining machine presents. Rides the BODY, not a header.

    :class:`apps.sync_hub.transport.HubTransport` is a two-method protocol
    with no header support, and a grant is not a bearer for ``push`` or
    ``pull``. Widening the transport for it would create a credential slot
    those endpoints would then be expected to fill.
    """

    kind: Literal["grant", "google_id_token"]
    value: str = Field(min_length=1)


class EnrollResponse(BaseModel):
    machine_id: str
    owner_google_sub: str
    owner_email: str
    hub_machine_id: str
    enrolled_at: str
    enrolled_via: str
    #: False when the machine was already enrolled to this owner and the call
    #: wrote nothing. Reported rather than inferred, so a re-run is visibly a
    #: no-op instead of a second success line hiding a duplicate.
    created: bool


def perform_enroll(
    conn: sqlite3.Connection,
    *,
    machine: MachineRow,
    credential: EnrollCredentialModel,
    hub_machine_id: str,
) -> EnrollResponse:
    """Resolve the credential, then enroll. Caller owns the transaction.

    Every raise below leaves the caller's transaction to roll back, so a
    refused enrollment writes nothing at all -- not the owner row, and not
    the ``machines`` row either.
    """
    try:
        resolved = enrollment_credentials.credential_from_wire(
            credential.kind, credential.value
        )
        owner = enrollment_credentials.resolve_enrollment_identity(
            conn, resolved, machine_id=machine.machine_id
        )
        result = enrollment.enroll_machine(
            conn,
            machine=machine,
            owner=owner,
            hub_machine_id=hub_machine_id,
            enrolled_via=enrollment_credentials.ENROLLED_VIA_BY_KIND[credential.kind],
        )
    except enrollment_credentials.EnrollmentCredentialUnavailable as exc:
        raise HTTPException(
            status_code=501,
            detail={"code": "SYNC_ENROLL_KIND_UNAVAILABLE", "message": str(exc)},
        ) from exc
    except enrollment_credentials.EnrollmentCredentialError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "SYNC_ENROLL_CREDENTIAL", "message": str(exc)},
        ) from exc
    except enrollment.OwnershipConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "SYNC_ENROLL_OWNER_CONFLICT", "message": str(exc)},
        ) from exc
    except enrollment.MachineRevokedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "SYNC_ENROLL_REVOKED", "message": str(exc)},
        ) from exc
    except enrollment.EnrollmentError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "SYNC_ENROLL", "message": str(exc)}
        ) from exc
    return EnrollResponse(
        machine_id=result.machine_id,
        owner_google_sub=result.owner.google_sub,
        owner_email=result.owner.email,
        hub_machine_id=result.hub_machine_id,
        enrolled_at=result.enrolled_at,
        enrolled_via=result.enrolled_via,
        created=result.created,
    )


__all__ = ["EnrollCredentialModel", "EnrollResponse", "perform_enroll"]
