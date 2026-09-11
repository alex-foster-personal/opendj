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
  503  the credential KIND is real but this hub cannot resolve it right now
       (google_id_token: no OAuth client id configured, or Google's JWKS
       unreachable)
  409  the machine is already owned by somebody else, its owner row is
       revoked, or the enrollment was otherwise refused

The three 409s carry different ``code`` values rather than one, because the
remedies differ: a conflict wants the rightful owner, a revocation wants a
deliberate decision to un-revoke, and the generic case wants the message.

503 rather than 401 when the hub cannot verify is deliberate. The remedy is
on the hub, and a user told their token is bad would re-sign-in forever
against a hub that has no client id. It used to be 501, while the kind was
reserved but unbuilt; the verifier landed in :mod:`apps.sync_hub.google_id_token`.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
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


class EnrollErrorBody(BaseModel):
    """The ``detail`` object every refusal below carries.

    Declared as a model rather than left implicit so the generated client can
    branch on ``code``. Every raise in :func:`enroll_machine_or_raise` builds
    exactly this shape; the codes are enumerated per status in
    :data:`ENROLL_RESPONSES`."""

    code: str
    message: str


class EnrollErrorResponse(BaseModel):
    """FastAPI wraps an ``HTTPException`` detail under ``detail``."""

    detail: EnrollErrorBody


#: What the route can answer with besides 200, for the OpenAPI document and
#: everything generated from it.
#:
#: Sol review, PR #1648 (P1 BLOCKING): the endpoint returned 401, 409 and 501
#: in production while the contract advertised only 200 and 422, so a
#: generated client could not tell a bad grant from an ownership conflict
#: from a kind that is reserved but unbuilt -- the three cases whose remedies
#: differ most. An undeclared status is a status callers have to discover by
#: hitting it.
ENROLL_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {
        "model": EnrollErrorResponse,
        "description": (
            "The credential did not establish an owner. code: "
            "SYNC_ENROLL_CREDENTIAL."
        ),
    },
    409: {
        "model": EnrollErrorResponse,
        "description": (
            "The machine is already owned by somebody else "
            "(SYNC_ENROLL_OWNER_CONFLICT), its owner row is revoked "
            "(SYNC_ENROLL_REVOKED), or the enrollment was otherwise refused "
            "(SYNC_ENROLL)."
        ),
    },
    503: {
        "model": EnrollErrorResponse,
        "description": (
            "The credential KIND is real but this hub cannot resolve it right "
            "now: for google_id_token, no OAuth client id is configured or "
            "Google's signing keys are unreachable. Never answered with an "
            "unverified acceptance. code: SYNC_ENROLL_KIND_UNAVAILABLE."
        ),
    },
}


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


def verify_credential(
    credential: EnrollCredentialModel,
) -> enrollment_credentials.VerifiedCredential:
    """Step 1, BEFORE the route opens its write transaction.

    Everything network-bound (a Google id_token's JWKS fetch) happens here,
    so the hub's sqlite write lock is never held across an outbound request
    an unauthenticated caller triggered. Refusals map to the same statuses
    as :func:`perform_enroll`'s.
    """
    with _enroll_errors_as_http():
        return enrollment_credentials.verify_outside_transaction(
            enrollment_credentials.credential_from_wire(credential.kind, credential.value)
        )


def perform_enroll(
    conn: sqlite3.Connection,
    *,
    machine: MachineRow,
    credential: enrollment_credentials.VerifiedCredential,
    hub_machine_id: str,
) -> EnrollResponse:
    """Step 2: resolve the verified credential, then enroll. Caller owns the transaction.

    Every raise below leaves the caller's transaction to roll back, so a
    refused enrollment writes nothing at all -- not the owner row, and not
    the ``machines`` row either.
    """
    with _enroll_errors_as_http():
        owner = enrollment_credentials.resolve_enrollment_identity(
            conn, credential, machine_id=machine.machine_id
        )
        result = enrollment.enroll_machine(
            conn,
            machine=machine,
            owner=owner,
            hub_machine_id=hub_machine_id,
            enrolled_via=enrollment_credentials.ENROLLED_VIA_BY_KIND[credential.KIND],
        )
    return EnrollResponse(
        machine_id=result.machine_id,
        owner_google_sub=result.owner.google_sub,
        owner_email=result.owner.email,
        hub_machine_id=result.hub_machine_id,
        enrolled_at=result.enrolled_at,
        enrolled_via=result.enrolled_via,
        created=result.created,
    )


@contextmanager
def _enroll_errors_as_http() -> Iterator[None]:
    """The one place an enrollment failure becomes an HTTP status."""
    try:
        yield
    except enrollment_credentials.EnrollmentCredentialUnavailable as exc:
        raise HTTPException(
            status_code=503,
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


__all__ = ["EnrollCredentialModel", "EnrollResponse", "perform_enroll", "verify_credential"]
