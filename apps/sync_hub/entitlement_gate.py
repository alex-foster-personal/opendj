"""The hub-side entitlement CHECK POINT for CloudSync (plan X6, Level 5 seam).

Two kinds of hub, one rule each:

  * SELF-HOSTED -- the only kind that exists today: a user's own daemon with
    ``MDT_IS_HUB`` set, on their own tailnet, against their own bucket. It is
    ALWAYS entitled and NEVER consults a source. Gating it would be the
    client-side DRM ``(private path)`` rejects permanently: the operator
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

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

from fastapi import HTTPException, Request
from pydantic import BaseModel

from apps import entitlements
from apps.entitlements import EntitlementSource, Operation
from apps.entitlements.lifecycle import allows
from apps.entitlements.polar import PolarEntitlementSource
from apps.shared.state import machine_identity
from apps.sync_hub import enrollment, hosted_config
from apps.sync_hub.hosted_config import HOSTED_FLAG_ATTR, SOURCE_ATTR

#: The feature a hosted hub asks about. Deliberately NOT in
#: ``apps.entitlements.catalog`` yet: the catalog is what the account panel
#: enumerates, and listing a hosted hub nobody can buy would render it as
#: "Included", which is fabricated product state (saas-spec section 6,
#: decision 1). It moves to the catalog when the hosted hub can meter.
HOSTED_HUB_FEATURE: str = "cloudsync.hosted_hub"
#: Hosted CloudStems (hub stem index and bundle presign reads). Polar maps a
#: product id to this feature via ``MDT_POLAR_PRODUCTS`` (see
#: ``(private path)``).
CLOUDSTEMS_HOSTED_FEATURE: str = "cloudstems.hosted"

#: Paid plugin catalog access on a hosted hub (CSPAY-06). Polar maps a product
#: id to this feature via ``MDT_POLAR_PRODUCTS``.
PLUGINSTORE_ACCESS_FEATURE: str = "pluginstore.access"

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
#: 503: a paid (Polar) source is configured but credentials are not ENFORCEd,
#: so the caller's machine_id is only a claim.
NOT_ENFORCED_CODE: str = "SYNC_HOSTED_NOT_ENFORCED"


class GateErrorBody(BaseModel):
    """The refusal. ``ui_title`` is present only on a plan refusal (ENT-02)."""

    code: str
    message: str
    ui_title: str | None = None


class GateErrorResponse(BaseModel):
    """FastAPI wraps an ``HTTPException`` detail under ``detail``."""

    detail: GateErrorBody


#: The gated endpoints, and whether each takes data out or puts it in.
GatedEndpoint = Literal[
    "push", "pull", "stems/index", "stems/bundle-presign", "stems/bundle-size", "lyrics-pair/upload-presign"
]
_OPERATION_OF: dict[GatedEndpoint, Operation] = {
    "push": "write",
    "lyrics-pair/upload-presign": "write",
    "pull": "read",
    "stems/index": "read",
    "stems/bundle-presign": "read",
    "stems/bundle-size": "read",
}

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
                f"hub DB holds more than one owner ({MULTI_OWNER_CODE}), or a "
                f"paid source runs without ENFORCEd credentials ({NOT_ENFORCED_CODE})."
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


def require_enforced(source: EntitlementSource) -> None:
    """503 when a paid source would be trusting an unauthenticated machine_id.

    Startup checks this too (:func:`hosted_config.require_enforce_for_polar`),
    but the credential mode is read per request, so a flip back to observe
    after startup must stop paid service rather than hand it out.
    """
    if not isinstance(source, PolarEntitlementSource):
        return
    reason = hosted_config.require_enforce_for_polar(os.environ)
    if reason is not None:
        raise _refusal(503, NOT_ENFORCED_CODE, reason)


def _refusal(status_code: int, code: str, message: str, *, ui_title: str | None = None) -> HTTPException:
    detail: dict[str, str] = {"code": code, "message": message}
    if ui_title is not None:
        detail["ui_title"] = ui_title
    return HTTPException(status_code=status_code, detail=detail)


def _unowned(machine_id: str) -> HTTPException:
    return _refusal(
        403,
        UNOWNED_CODE,
        f"machine {machine_id} has no owner this hosted hub honors "
        "(never enrolled, revoked, or enrolled by another hub). Enroll it "
        "with POST /api/v1/sync/enroll; entitlement is checked per owner.",
    )


def require(
    request: Request,
    conn: sqlite3.Connection,
    *,
    machine_id: str,
    operation: Operation,
    data_dir: Callable[[], Path],
    feature_id: str = HOSTED_HUB_FEATURE,
) -> str | None:
    """Refuse ``operation`` for ``machine_id`` unless its owner is entitled.

    Returns the subject of the owner whose entitlement passed, or None on a
    self-hosted hub. A caller that bills must bill THIS subject: reading
    enrollment a second time can see a different owner after a concurrent
    reassignment, who was never checked (Sol review of f9e7f65676, P1).

    A no-op on a self-hosted hub, and it returns BEFORE touching the source,
    the ownership table or the hub's data dir -- so a self-hosted hub answers
    exactly as it did before this check point existed. ``data_dir`` is a
    callable for that reason: hosted mode is the only mode that pays for it.
    The hub's own id comes from the same ``machine-id`` file ``/enroll``
    stamped onto every owner row, so the hub-binding guard applies here too.
    """
    if not is_hosted(request):
        return None
    source = _source(request)
    require_enforced(source)
    shared = hosted_config.too_many_owners(hosted_config.owner_count(conn))
    if shared is not None:
        raise _refusal(503, MULTI_OWNER_CODE, shared)
    hub_machine_id = machine_identity.get_or_create_machine_id(data_dir())
    owner = enrollment.owner_for(conn, machine_id, hub_machine_id=hub_machine_id)
    if owner is None:
        raise _unowned(machine_id)
    try:
        found = entitlements.standing(feature_id, subject=owner.google_sub, source=source)
    except entitlements.EntitlementProviderError as exc:
        raise _refusal(503, PROVIDER_CODE, str(exc)) from exc
    if found is not None and allows(found.state, operation):
        return owner.google_sub
    where = "no plan on record" if found is None else f"state {found.state}"
    raise _refusal(
        403,
        entitlements.NOT_IN_PLAN_CODE,
        f"{operation} refused for machine {machine_id}: its owner has {where} "
        f"for {feature_id} (source {source.provider}). Nothing on "
        f"the hub was removed. {entitlements.NOT_IN_PLAN_MESSAGE}",
        ui_title=entitlements.UI_REFUSAL_TITLE,
    )


def status_fields(request: Request) -> dict[str, bool | str | None]:
    """``hosted`` and ``entitlement_provider`` for ``/sync/status`` (agent parity)."""
    source = getattr(request.app.state, SOURCE_ATTR, None)
    hosted = is_hosted(request)
    provider = cast(EntitlementSource, source).provider if hosted and source is not None else None
    return {"hosted": hosted, "entitlement_provider": provider}


def merge_responses(*parts: dict[int | str, dict[str, object]]) -> dict[int | str, dict[str, object]]:
    """Compose OpenAPI ``responses`` dicts without losing a colliding status.

    ``{**a, **b}`` keeps only ``b``'s entry for a status both declare, which
    silently drops a refusal the route can still return. Colliding entries keep
    both descriptions and every other field either declares; a field both
    declare with different values (two body models, say) is a contract error
    and raises.
    """
    merged: dict[int | str, dict[str, object]] = {}
    for part in parts:
        for key, entry in part.items():
            # 503 and "503" are one OpenAPI status, so they must collide here too.
            status: int | str = int(key) if isinstance(key, str) and key.isdigit() else key
            prior = merged.get(status)
            if prior is None:
                merged[status] = dict(entry)
                continue
            combined = dict(prior)
            for field, value in entry.items():
                if field == "description":
                    continue
                if field in combined and combined[field] != value:
                    raise ValueError(f"status {status} declares two different values for {field!r}")
                combined[field] = value
            combined["description"] = f"{prior.get('description', '')} Or: {entry.get('description', '')}".strip()
            merged[status] = combined
    return merged


__all__ = [
    "CLOUDSTEMS_HOSTED_FEATURE",
    "FLAG_INVALID_CODE",
    "HOSTED_FLAG_ATTR",
    "HOSTED_HUB_FEATURE",
    "MULTI_OWNER_CODE",
    "NOT_ENFORCED_CODE",
    "NO_SOURCE_CODE",
    "PLUGINSTORE_ACCESS_FEATURE",
    "PROVIDER_CODE",
    "SOURCE_ATTR",
    "UNOWNED_CODE",
    "GateErrorBody",
    "GateErrorResponse",
    "GatedEndpoint",
    "Operation",
    "is_hosted",
    "merge_responses",
    "refusals",
    "require",
    "require_enforced",
    "status_fields",
]
