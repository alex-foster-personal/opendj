"""The hub-side entitlement CHECK POINT for CloudSync (plan X6, Level 5 seam).

Two kinds of hub, one rule each:

  * SELF-HOSTED -- the only kind that exists today: a user's own daemon with
    ``MDT_IS_HUB`` set, on their own tailnet, against their own bucket. It is
    ALWAYS entitled and NEVER consults a source. Gating it would be the
    client-side DRM ``specs/saas-spec.md`` rejects permanently: the operator
    is the user, and the flag is one line in source they already hold.
  * HOSTED -- a hub we operate (``MDT_SYNC_HUB_HOSTED=1``, default off, read
    strictly at startup by :mod:`apps.sync_hub.hosted_config` into
    ``app.state.sync_hub_hosted``). There the server refusing work IS the
    enforcement (BILL-01), so the
    configured ``app.state.entitlement_source`` is asked where the calling
    machine's ENROLLED OWNER stands, and :mod:`apps.entitlements.lifecycle`
    decides whether that state admits the operation.

What each lifecycle state means at this check point:

    active / past_due   push, pull
    read_only           pull               (push -> 403 entitlement_not_in_plan)
    archived            neither            (every row is RETAINED)

Only the two endpoints that MOVE rows are gated. ``status`` and ``digest``
answer in every state: they report where a machine stands (counts, seq,
per-table hashes of the caller's own data) and never transfer a row, so a
lapsed client can always see why its sync stopped.

Nothing here writes. A refusal is a refusal of WORK: no row, owner, machine
or changelog entry is removed or changed by any state, which is what lets a
resubscribed user pick up exactly where they stopped.

WHAT THIS IS NOT, YET.  The caller is still identified by the ``machine_id``
it names, and ADR 12 enrollment is still OBSERVE-only. Until the per-machine
hub credential lands (plan gap: no per-request caller authentication), a
hosted hub gated here refuses a lapsed owner's own machine but cannot stop
someone who learned a paying machine's id. This module is the seam that
credential plugs into, not a claim that the hub is authenticated.

ONE HUB DB PER OWNER.  Pull is gated per owner but NOT scoped per owner:
``engine.hub_changes_since`` returns every changelog row on the hub. A hosted
hub DB shared by two owners would therefore hand each the other's library,
so a hosted hub refuses to serve a DB holding more than one owner (503
``SYNC_HOSTED_MULTI_OWNER``, checked here per request and by
:mod:`apps.sync_hub.hosted_config` at startup) until pull is owner-scoped.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

from fastapi import HTTPException, Request
from pydantic import BaseModel

from apps import entitlements
from apps.entitlements import EntitlementSource, Operation
from apps.entitlements.lifecycle import allows
from apps.shared.state import machine_identity
from apps.sync_hub import enrollment, hosted_config
from apps.sync_hub.hosted_config import HOSTED_FLAG_ATTR, SOURCE_ATTR

#: The feature a hosted hub asks about. Deliberately NOT in
#: ``apps.entitlements.catalog`` yet: the catalog is what the account panel
#: enumerates, and listing a hosted hub nobody can buy would render it as
#: "Included", which is fabricated product state (saas-spec section 6,
#: decision 1). It moves to the catalog when the hosted hub can meter.
HOSTED_HUB_FEATURE: str = "cloudsync.hosted_hub"

#: 403: the caller's machine has no owner this hub honors, so there is no
#: subject to ask a source about -- and a source is never asked about nobody.
UNOWNED_CODE: str = "SYNC_HOSTED_UNOWNED"

#: 503: hosted mode is on but the hub cannot answer entitlement questions.
NO_SOURCE_CODE: str = "SYNC_HOSTED_NO_SOURCE"
PROVIDER_CODE: str = "SYNC_ENTITLEMENT_PROVIDER"
#: 503: the hosted flag on app.state is not a bool, so nobody can say which
#: kind of hub this is. Distinct from NO_SOURCE_CODE: a source may be set.
FLAG_INVALID_CODE: str = "SYNC_HOSTED_FLAG_INVALID"
#: 503: the hub DB holds more than one owner, and pull is not owner-scoped.
MULTI_OWNER_CODE: str = "SYNC_HOSTED_MULTI_OWNER"


class GateErrorBody(BaseModel):
    """The refusal. ``ui_title`` is present only on a plan refusal (ENT-02)."""

    code: str
    message: str
    ui_title: str | None = None


class GateErrorResponse(BaseModel):
    """FastAPI wraps an ``HTTPException`` detail under ``detail``."""

    detail: GateErrorBody


#: The gated endpoints, and whether each takes data out or puts it in.
GatedEndpoint = Literal["push", "pull"]
_OPERATION_OF: dict[GatedEndpoint, Operation] = {"push": "write", "pull": "read"}

#: The lifecycle states that refuse each operation, for the declared docs.
_REFUSED_IN: dict[Operation, str] = {
    "write": "read_only or archived",
    "read": "archived (never read_only: a lapsed user can always pull)",
}


def refusals(endpoint: GatedEndpoint) -> dict[int | str, dict[str, object]]:
    """What ``endpoint`` can answer besides its own statuses, for OpenAPI.

    Per endpoint rather than one shared constant, because the honest answer
    differs: push is refused in ``read_only`` and pull is not.
    """
    operation = _OPERATION_OF[endpoint]
    return {
        403: {
            "model": GateErrorResponse,
            "description": (
                f"HOSTED hubs only. {endpoint} is refused with "
                f"{entitlements.NOT_IN_PLAN_CODE} when the calling machine's "
                f"owner is {_REFUSED_IN[operation]}, or has no plan on record "
                f"(data is always retained); or with {UNOWNED_CODE} when the "
                f"machine has no owner this hub honors."
            ),
        },
        503: {
            "model": GateErrorResponse,
            "description": (
                f"HOSTED hubs only. {endpoint} cannot be decided: no "
                f"entitlement source is configured ({NO_SOURCE_CODE}), the "
                f"entitlement provider refused to answer ({PROVIDER_CODE}), "
                f"the hosted flag is not a bool ({FLAG_INVALID_CODE}), or the "
                f"hub DB holds more than one owner ({MULTI_OWNER_CODE})."
            ),
        },
    }


def is_hosted(request: Request) -> bool:
    """True only when the app EXPLICITLY declares itself a hosted hub.

    The attribute must be a real bool: a truthy string such as ``"0"`` would
    otherwise switch billing enforcement on by accident.
    """
    flag = getattr(request.app.state, HOSTED_FLAG_ATTR, False)
    if not isinstance(flag, bool):
        raise HTTPException(
            status_code=503,
            detail={
                "code": FLAG_INVALID_CODE,
                "message": (
                    f"app.state.{HOSTED_FLAG_ATTR} must be a bool, got "
                    f"{flag!r}; refusing to guess whether this hub is hosted."
                ),
            },
        )
    return flag


def _source(request: Request) -> EntitlementSource:
    source = getattr(request.app.state, SOURCE_ATTR, None)
    if source is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": NO_SOURCE_CODE,
                "message": (
                    f"app.state.{HOSTED_FLAG_ATTR} is True but "
                    f"app.state.{SOURCE_ATTR} is unset. A hosted hub with no "
                    "entitlement source cannot say who may sync, and will not "
                    "guess. Configure a source, or unset the hosted flag to "
                    "run a self-hosted hub (always entitled)."
                ),
            },
        )
    return cast(EntitlementSource, source)


def _refusal(
    status_code: int, code: str, message: str, *, ui_title: str | None = None
) -> HTTPException:
    detail: dict[str, str] = {"code": code, "message": message}
    if ui_title is not None:
        detail["ui_title"] = ui_title
    return HTTPException(status_code=status_code, detail=detail)


def require(
    request: Request,
    conn: sqlite3.Connection,
    *,
    machine_id: str,
    operation: Operation,
    data_dir: Callable[[], Path],
) -> None:
    """Refuse ``operation`` for ``machine_id`` unless its owner is entitled.

    A no-op on a self-hosted hub, and it returns BEFORE touching the source,
    the ownership table or the hub's data dir -- so a self-hosted hub answers
    exactly as it did before this check point existed. ``data_dir`` is a
    callable for that reason: hosted mode is the only mode that pays for it.
    The hub's own id comes from the same ``machine-id`` file ``/enroll``
    stamped onto every owner row, so the hub-binding guard applies here too.
    """
    if not is_hosted(request):
        return
    source = _source(request)
    shared = hosted_config.too_many_owners(hosted_config.owner_count(conn))
    if shared is not None:
        raise _refusal(503, MULTI_OWNER_CODE, shared)
    hub_machine_id = machine_identity.get_or_create_machine_id(data_dir())
    owner = enrollment.owner_for(conn, machine_id, hub_machine_id=hub_machine_id)
    if owner is None:
        raise _refusal(
            403,
            UNOWNED_CODE,
            f"machine {machine_id} has no owner this hosted hub honors "
            "(never enrolled, revoked, or enrolled by another hub). Enroll it "
            "with POST /api/v1/sync/enroll; entitlement is checked per owner.",
        )
    try:
        found = entitlements.standing(HOSTED_HUB_FEATURE, subject=owner.google_sub, source=source)
    except entitlements.EntitlementProviderError as exc:
        raise _refusal(503, PROVIDER_CODE, str(exc)) from exc
    if found is not None and allows(found.state, operation):
        return
    where = "no plan on record" if found is None else f"state {found.state}"
    raise _refusal(
        403,
        entitlements.NOT_IN_PLAN_CODE,
        f"{operation} refused for machine {machine_id}: its owner has {where} "
        f"for {HOSTED_HUB_FEATURE} (source {source.provider}). Nothing on "
        f"the hub was removed. {entitlements.NOT_IN_PLAN_MESSAGE}",
        ui_title=entitlements.UI_REFUSAL_TITLE,
    )


def status_fields(request: Request) -> dict[str, bool | str | None]:
    """``hosted`` and ``entitlement_provider`` for ``/sync/status`` (agent parity)."""
    source = getattr(request.app.state, SOURCE_ATTR, None)
    hosted = is_hosted(request)
    provider = cast(EntitlementSource, source).provider if hosted and source is not None else None
    return {"hosted": hosted, "entitlement_provider": provider}


__all__ = [
    "FLAG_INVALID_CODE",
    "HOSTED_FLAG_ATTR",
    "HOSTED_HUB_FEATURE",
    "MULTI_OWNER_CODE",
    "NO_SOURCE_CODE",
    "PROVIDER_CODE",
    "SOURCE_ATTR",
    "UNOWNED_CODE",
    "GateErrorBody",
    "GateErrorResponse",
    "GatedEndpoint",
    "Operation",
    "is_hosted",
    "refusals",
    "require",
    "status_fields",
]
