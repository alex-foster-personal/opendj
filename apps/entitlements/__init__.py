"""THE entitlement seam: the only way code asks what an account may do.

Public interface, and deliberately only two functions (ENT-01):

    entitlements.has(feature_id)   -> bool
    entitlements.quota(feature_id) -> int | None

Everything else exported here is the REFUSAL SHAPE (code, message, ui_title)
and the plan vocabulary, so a route can report a refusal without inventing
its own wording.  Nothing outside this package may branch on a plan name --
``if plan == "pro"`` hardcodes pricing into business logic, turns comped and
grandfathered accounts into special cases, and forces a redeploy to change a
price.  ``tests/entitlements/test_plan_name_branching.py`` greps for exactly
that and fails on it.

WHY THIS SHIPS INERT (ENT-04).  openDJ has no paid features.  Enforcing one
inside an Apache-2.0 app the user compiles and runs themselves is not a
solvable engineering problem, so the governing rule in ``specs/saas-spec.md``
is that a paid feature must be one that needs a server we run -- then the
server refusing work IS the enforcement and no client-side check exists to
flip.  Until such a feature exists, the resolver returns entitled for
everything and is wired to no payment provider.  That is the honest inert
state, not a stub that fakes a plan.

WHERE THIS SITS.  A domain package BELOW ``apps.webui`` and
``apps.engine_core``, per ``.importlinter``: ``apps.shared`` must never import
it and it must never import the web UI.  The HTTP surface that reports it
lives in ``apps.engine_core.account.api``; this package decides, that route
reports, exactly like ``apps.shared.rekordbox_writeback`` and
``apps/webui/server/routes/rekordbox_gate.py``.

FEATURE FLAGS ARE NOT ENTITLEMENTS (FLAG-01).  They live in
``apps.feature_flags``, in their own file, with their own API.  A flag answers
"is this code path enabled" and is engineering-owned, deploy-time and
temporary; an entitlement answers "is this account allowed" and is
billing-owned and persistent.
"""

from __future__ import annotations

from apps.entitlements.catalog import FEATURES, FEATURES_BY_ID, Feature
from apps.entitlements.resolver import (
    NOT_IN_PLAN_CODE,
    NOT_IN_PLAN_MESSAGE,
    PROVIDER_ENV,
    UI_REFUSAL_TITLE,
    UNGATED_PLAN,
    EntitlementProviderError,
    Plan,
    configured_provider,
    current_plan,
    has,
    quota,
)

__all__ = [
    "FEATURES",
    "FEATURES_BY_ID",
    "NOT_IN_PLAN_CODE",
    "NOT_IN_PLAN_MESSAGE",
    "PROVIDER_ENV",
    "UI_REFUSAL_TITLE",
    "UNGATED_PLAN",
    "EntitlementProviderError",
    "Feature",
    "Plan",
    "configured_provider",
    "current_plan",
    "has",
    "quota",
]
