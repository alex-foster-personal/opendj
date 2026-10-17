"""Billing routes on a hosted, Polar-backed hub: checkout, portal, refresh.

Three bearer-authenticated endpoints under ``/sync/billing``, each resolving
the calling machine to its enrolled owner (the Google ``sub`` that is also
the Polar customer ``external_id``) before it touches Polar:

  POST /sync/billing/checkout  a hosted Polar checkout URL for one feature
  POST /sync/billing/portal    Polar's customer portal (card, cancel, invoices)
  POST /sync/billing/refresh   reconcile the owner's full Polar state NOW and
                               report the standing per sold feature

``refresh`` is how fulfilment happens after checkout (BILL-03, BILL-04): the
client calls it once the user is back, and the hub pulls Polar's full state
instead of trusting the browser redirect or waiting on a webhook. Card data
never reaches the hub (BILL-06): checkout and the portal are Polar-hosted.

Every route answers 409 ``SYNC_BILLING_NOT_CONFIGURED`` on a hub that is not
hosted or whose source is not Polar (a self-hosted hub sells nothing), and
503 ``SYNC_ENTITLEMENT_PROVIDER`` when Polar cannot answer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.entitlements.lifecycle import LifecycleState
from apps.entitlements.polar import PolarClient, PolarEntitlementSource, PolarError, PolarNoCustomer
from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.sync_hub import enrollment, entitlement_gate, hosted_config, service_credentials
from apps.sync_hub.enrollment import OwnerIdentity
from apps.sync_hub.hosted_config import SOURCE_ATTR
from apps.sync_hub.machine_wire_limits import MACHINE_ID_MAX_LENGTH
from apps.sync_hub.service_request import _data_dir, _db_path

router = APIRouter(prefix="/billing", tags=["sync-billing"])

NOT_CONFIGURED_CODE: str = "SYNC_BILLING_NOT_CONFIGURED"
#: The portal was asked for by an owner Polar has never billed: there is nothing to manage yet.
NO_CUSTOMER_CODE: str = "SYNC_BILLING_NO_CUSTOMER"
UNKNOWN_FEATURE_CODE: str = "SYNC_BILLING_UNKNOWN_FEATURE"


def _responses(endpoint: str) -> dict[int | str, dict[str, object]]:
    credential = service_credentials.credential_responses(endpoint)
    return {
        **credential,
        403: {
            "description": (
                f"{endpoint}: the machine has no owner this hub honors. code: {entitlement_gate.UNOWNED_CODE}."
            )
        },
        409: {
            "description": (
                f"{endpoint}: this hub sells nothing (not hosted, or not Polar-backed), "
                f"code {NOT_CONFIGURED_CODE}; or the feature is not sold here, code "
                f"{UNKNOWN_FEATURE_CODE}; or (portal) Polar has never billed this owner, code "
                f"{NO_CUSTOMER_CODE}."
            )
        },
        # Every 503 the handlers raise, not only the credential layer's (#6833 Sol P1).
        503: {
            "description": (
                f"{credential[503]['description']} Or: the hosted hub does not enforce "
                f"credentials, code {entitlement_gate.NOT_ENFORCED_CODE}; or it holds more "
                f"than one owner, code {entitlement_gate.MULTI_OWNER_CODE}; or Polar could "
                f"not answer, code {entitlement_gate.PROVIDER_CODE}."
            )
        },
    }


class BillingRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    machine_id: str = Field(min_length=1, max_length=MACHINE_ID_MAX_LENGTH)


class CheckoutRequest(BillingRequest):
    feature_id: str = Field(min_length=1, max_length=128)


class BillingUrlResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    provider: str


class FeatureStanding(BaseModel):
    model_config = ConfigDict(frozen=True)

    feature_id: str
    #: None: Polar holds no subscription for this feature (never subscribed).
    state: LifecycleState | None


class RefreshResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    standings: list[FeatureStanding]


@contextmanager
def _hub_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = state_db.open_rw(_db_path(request))
    try:
        yield conn
    finally:
        conn.close()


def _polar_source(request: Request) -> PolarEntitlementSource:
    if not entitlement_gate.is_hosted(request):
        raise HTTPException(
            status_code=409,
            detail={
                "code": NOT_CONFIGURED_CODE,
                "message": "this is a self-hosted hub; it is free and sells nothing.",
            },
        )
    source = getattr(request.app.state, SOURCE_ATTR, None)
    if not isinstance(source, PolarEntitlementSource):
        raise HTTPException(
            status_code=409,
            detail={
                "code": NOT_CONFIGURED_CODE,
                "message": (
                    "this hosted hub's entitlement source is not Polar, so it has "
                    "no checkout. Set MDT_SYNC_HUB_ENTITLEMENTS_PROVIDER=polar."
                ),
            },
        )
    entitlement_gate.require_enforced(source)
    return source


def _owner(request: Request, machine_id: str, endpoint: str, *, require_valid: bool = False) -> OwnerIdentity:
    with _hub_conn(request) as conn:
        verdict = service_credentials.require_credential(
            request, conn, machine_id, data_dir=_data_dir(request), endpoint=endpoint
        )
        # Observe mode only logs a bad credential. A paid route that opts in
        # refuses anything but a valid one in either mode; billing keeps the
        # ENFORCE-only 401 its contract declares.
        if require_valid and verdict != "valid":
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "SYNC_CREDENTIAL",
                    "message": (
                        f"{endpoint} refused: the sync credential is {verdict}. Hosted "
                        "billing and paid routes need this machine's credential from "
                        "`python -m apps.sync_hub enroll` (<data-dir>/sync-credential)."
                    ),
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        # The same per-request single-owner rule the sync gate applies: never
        # sell a plan on a hub that cannot serve its buyer.
        shared = hosted_config.too_many_owners(hosted_config.owner_count(conn))
        if shared is not None:
            raise HTTPException(status_code=503, detail={"code": entitlement_gate.MULTI_OWNER_CODE, "message": shared})
        hub_machine_id = machine_identity.get_or_create_machine_id(_data_dir(request))
        owner = enrollment.owner_for(conn, machine_id, hub_machine_id=hub_machine_id)
    if owner is None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": entitlement_gate.UNOWNED_CODE,
                "message": (
                    f"machine {machine_id} has no owner this hub honors; enroll it "
                    "with POST /api/v1/sync/enroll before buying a plan."
                ),
            },
        )
    return owner


def _provider_error(exc: PolarError) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"code": entitlement_gate.PROVIDER_CODE, "message": str(exc)},
    )


@router.post("/checkout", response_model=BillingUrlResponse, responses=_responses("checkout"))
def checkout(request: Request, payload: CheckoutRequest) -> BillingUrlResponse:
    """A Polar-hosted checkout URL for ``feature_id``, tied to the machine's owner."""
    source = _polar_source(request)
    owner = _owner(request, payload.machine_id, "checkout")  # Authenticate before revealing the product map.
    if not source.config.products_for(payload.feature_id):
        raise HTTPException(
            status_code=409,
            detail={
                "code": UNKNOWN_FEATURE_CODE,
                "message": (
                    f"feature {payload.feature_id!r} is not sold on this hub; sold: "
                    f"{sorted(set(source.config.product_features.values()))}"
                ),
            },
        )
    try:
        url = PolarClient(source.config).create_checkout(owner.google_sub, payload.feature_id, email=owner.email)
    except PolarError as exc:
        raise _provider_error(exc) from exc
    return BillingUrlResponse(url=url, provider=source.provider)


@router.post("/portal", response_model=BillingUrlResponse, responses=_responses("portal"))
def portal(request: Request, payload: BillingRequest) -> BillingUrlResponse:
    """Polar's customer portal for the machine's owner."""
    source = _polar_source(request)
    owner = _owner(request, payload.machine_id, "portal")
    try:
        url = PolarClient(source.config).customer_portal_url(owner.google_sub)
    except PolarNoCustomer as exc:
        raise HTTPException(status_code=409, detail={"code": NO_CUSTOMER_CODE, "message": str(exc)}) from exc
    except PolarError as exc:
        raise _provider_error(exc) from exc
    return BillingUrlResponse(url=url, provider=source.provider)


@router.post("/refresh", response_model=RefreshResponse, responses=_responses("refresh"))
def refresh(request: Request, payload: BillingRequest) -> RefreshResponse:
    """Reconcile the owner's full Polar state now, then report every sold feature."""
    source = _polar_source(request)
    owner = _owner(request, payload.machine_id, "refresh")
    try:
        # Every row from this one answer: a concurrent refresh may replace the snapshot.
        subscriptions = source.reconcile(owner.google_sub)
        standings = [
            FeatureStanding(
                feature_id=feature_id,
                state=None if found is None else cast(LifecycleState, found.state),
            )
            for feature_id in sorted(set(source.config.product_features.values()))
            for found in [source.standing_in(subscriptions, feature_id)]
        ]
    except PolarError as exc:
        raise _provider_error(exc) from exc
    return RefreshResponse(provider=source.provider, standings=standings)


__all__ = [
    "NOT_CONFIGURED_CODE",
    "UNKNOWN_FEATURE_CODE",
    "BillingUrlResponse",
    "CheckoutRequest",
    "RefreshResponse",
    "router",
]
