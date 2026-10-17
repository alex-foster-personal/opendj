"""Spoke proxy for hosted Polar billing (CSPAY-04).

Mounted on non-hosted engines BEFORE :mod:`apps.sync_hub.service_billing`, so
a spoke's ``POST /api/v1/sync/billing/*`` reaches the configured hub with the
sync credential instead of answering 409 locally.
"""

from __future__ import annotations

from typing import TypeVar

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from apps.sync_hub import spoke_billing
from apps.sync_hub.service_billing import BillingUrlResponse, RefreshResponse
from apps.webui.server.routes.cloudsync_status import data_dir_for_request


class SpokeBillingBody(BaseModel):
    # No machine_id: the proxy always sends this spoke's own id with its own
    # credential, so a caller cannot name another machine.
    model_config = ConfigDict(frozen=True, extra="forbid")


class SpokeCheckoutBody(SpokeBillingBody):
    feature_id: str = Field(min_length=1, max_length=128)


class EligibilityResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    eligible: bool
    #: None: no hub is configured, so nothing says whether one is hosted.
    hosted: bool | None
    entitlement_provider: str | None


router = APIRouter(prefix="/sync", tags=["sync", "sync-billing"])

_Answer = TypeVar("_Answer", bound=BaseModel)


def _proxy_response(hub: spoke_billing.HubJsonResponse) -> JSONResponse:
    return JSONResponse(status_code=hub.status_code, content=hub.body)


def _hub_answer(hub: spoke_billing.HubJsonResponse, model: type[_Answer]) -> _Answer | JSONResponse:
    """A hub error passes through as is; a 200 must match the hub's published model."""
    if hub.status_code != 200:
        return _proxy_response(hub)
    try:
        return model.model_validate(hub.body)
    except ValidationError as exc:
        # The published contract is the hub's model: a drifted answer fails here
        # instead of reaching the account panel in a shape nothing declared.
        raise HTTPException(
            status_code=502,
            detail={
                "code": spoke_billing.PROTOCOL_CODE,
                "message": f"the CloudSync hub billing answer does not match {model.__name__}",
            },
        ) from exc


def _handle_spoke_billing_error(exc: spoke_billing.SpokeBillingError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


@router.get("/billing/eligibility", response_model=EligibilityResponse)
def billing_eligibility(request: Request) -> JSONResponse | EligibilityResponse:
    """Whether this spoke's hub is a hosted Polar hub (for the account panel)."""
    data_dir = data_dir_for_request(request)
    try:
        status = spoke_billing.hub_sync_status(data_dir)
    except spoke_billing.SpokeBillingError as exc:
        # No hub configured is the normal state for a local-only user, not an error.
        if exc.code == spoke_billing.NOT_CONFIGURED_CODE:
            return EligibilityResponse(eligible=False, hosted=None, entitlement_provider=None)
        _handle_spoke_billing_error(exc)
    if status.status_code != 200:
        return _proxy_response(status)
    hosted = status.body.get("hosted")
    provider = status.body.get("entitlement_provider")
    if (
        not isinstance(hosted, bool)
        or "entitlement_provider" not in status.body
        or not (provider is None or isinstance(provider, str))
    ):
        # A hub contract regression must surface, not read as "not eligible".
        raise HTTPException(
            status_code=502,
            detail={
                "code": spoke_billing.PROTOCOL_CODE,
                "message": "the CloudSync hub status has no boolean hosted or no string-or-null entitlement_provider",
            },
        )
    eligible = hosted and isinstance(provider, str) and (provider == "polar" or provider.startswith("polar:"))
    return EligibilityResponse(eligible=eligible, hosted=hosted, entitlement_provider=provider)


@router.post(
    "/billing/checkout",
    response_model=BillingUrlResponse,
    responses={409: {"description": "SYNC_BILLING_NOT_CONFIGURED or unknown feature"}},
)
def billing_checkout(request: Request, payload: SpokeCheckoutBody) -> JSONResponse | BillingUrlResponse:
    data_dir = data_dir_for_request(request)
    body = payload.model_dump(mode="json")
    try:
        hub = spoke_billing.post_billing(data_dir, "checkout", body)
    except spoke_billing.SpokeBillingError as exc:
        _handle_spoke_billing_error(exc)
    return _hub_answer(hub, BillingUrlResponse)


@router.post("/billing/portal", response_model=BillingUrlResponse)
def billing_portal(request: Request, payload: SpokeBillingBody) -> JSONResponse | BillingUrlResponse:
    data_dir = data_dir_for_request(request)
    body = payload.model_dump(mode="json")
    try:
        hub = spoke_billing.post_billing(data_dir, "portal", body)
    except spoke_billing.SpokeBillingError as exc:
        _handle_spoke_billing_error(exc)
    return _hub_answer(hub, BillingUrlResponse)


@router.post("/billing/refresh", response_model=RefreshResponse)
def billing_refresh(request: Request, payload: SpokeBillingBody) -> JSONResponse | RefreshResponse:
    data_dir = data_dir_for_request(request)
    body = payload.model_dump(mode="json")
    try:
        hub = spoke_billing.post_billing(data_dir, "refresh", body)
    except spoke_billing.SpokeBillingError as exc:
        _handle_spoke_billing_error(exc)
    return _hub_answer(hub, RefreshResponse)


__all__ = ["router"]
