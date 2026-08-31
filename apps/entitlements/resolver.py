"""The DECIDER behind :func:`has` and :func:`quota`.

THE REFUSAL SHAPE (ENT-02).  Every "no" this module can produce carries three
things, spelled once here:

  * :data:`NOT_IN_PLAN_CODE`    -- stable, machine-branchable, never reworded
  * :data:`NOT_IN_PLAN_MESSAGE` -- the human sentence, for a toast or a body
  * :data:`UI_REFUSAL_TITLE`    -- the exact tooltip a disabled control carries

They travel together over HTTP so a disabled control and the server refusing
the request can never disagree about the reason, which is the same contract
``apps.shared.rekordbox_writeback`` holds for the one-way import gate.

THE THIRD STATE (ENT-03).  :data:`UI_REFUSAL_TITLE` is deliberately NOT the
"not implemented - see PARITY-TODO" wording (which means NOT BUILT) and NOT
the capability refusals in
``apps/webui/frontend/src/lib/api/capabilities.svelte.ts`` (which mean THIS
DAEMON DOES NOT OFFER IT).  "Not on your plan" is a third fact and gets a
third sentence; conflating them tells the user a lie about why a control is
dead.  ``apps/webui/frontend/tests/unit/plan-refusal.test.mjs`` pins the string
on both sides of the wire.

NO PROVIDER, NO GATE (ENT-04).  ``MDT_ENTITLEMENTS_PROVIDER`` unset or blank is
the shipped state and means: no payment provider, no paid features, everything
entitled, unlimited.  Set it to anything at all and every call raises
:class:`EntitlementProviderError` with the runbook, because no provider is
implemented -- all BILL-* requirements are deferred.  That is the house
fail-fast rule (auth's 503 runbook, assistant's 409, cloud's MissingEnvError):
a misconfigured seam must stop, never quietly keep answering "yes" and let an
operator believe billing is live.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: The one switch. Unset or blank -> inert. Any value -> fail fast, because
#: no provider integration exists to name.
PROVIDER_ENV: str = "MDT_ENTITLEMENTS_PROVIDER"

#: The stable code every plan refusal carries, on the wire and in the
#: exception. Callers branch on this, never on the prose.
NOT_IN_PLAN_CODE: str = "entitlement_not_in_plan"

NOT_IN_PLAN_MESSAGE: str = (
    "this feature is not included in the plan on your account. Open the "
    "account panel from the user bauble, or GET /api/v1/account, to see the "
    "current plan and everything it includes."
)

#: The exact tooltip a plan-gated control carries. A THIRD distinct sentence:
#: not the PARITY-TODO wording (not built), not the capability refusals (this
#: daemon does not offer it). Mirrored verbatim in
#: apps/webui/frontend/src/lib/api/entitlements.svelte.ts and pinned by a test
#: on each side.
UI_REFUSAL_TITLE: str = (
    "not included in your plan - see your account for what is included"
)

#: The plan id reported while no payment provider is configured. NOT "free":
#: a free tier implies a paid one to graduate to, and there is not one. ENT-05
#: fixes the vocabulary as plan / entitlement / feature -- never "tier", which
#: the stem separation quality ladder already owns.
PLAN_ID_UNGATED: str = "ungated"

PLAN_LABEL_UNGATED: str = "Ungated - every feature is included"

PLAN_NOTE_UNGATED: str = (
    "openDJ has no paid features and this daemon is wired to no payment "
    "provider, so nothing is gated. This is not the free tier of a paid "
    "product; there is no paid product. Signing in gives openDJ your "
    "identity, never authorisation: the app works exactly the same signed "
    "out."
)


@dataclass(frozen=True)
class Plan:
    """What an account is on, as the API reports it.

    ``provider`` is None in the shipped state and is the field that tells a
    reader whether ``plan_id`` came from anywhere real.  A plan with no
    provider behind it gates nothing, and ``note`` says so in words.
    """

    plan_id: str
    label: str
    note: str
    provider: str | None


UNGATED_PLAN: Plan = Plan(
    plan_id=PLAN_ID_UNGATED,
    label=PLAN_LABEL_UNGATED,
    note=PLAN_NOTE_UNGATED,
    provider=None,
)


class EntitlementProviderError(RuntimeError):
    """``MDT_ENTITLEMENTS_PROVIDER`` names a provider that does not exist."""


def _runbook(raw: str) -> str:
    return (
        f"{PROVIDER_ENV}={raw!r} but no entitlement provider is implemented in "
        "this build. Every BILL-* requirement in specs/saas-spec.md is "
        "DEFERRED on purpose: there is no paid feature to gate, and payment "
        "plumbing built before one exists is a toll booth before a road. "
        f"RUNBOOK: unset {PROVIDER_ENV} (or set it to an empty string) to run "
        "the shipped ungated state, in which every feature is included. Do "
        "NOT set it hoping for a soft fallback -- this seam will not guess a "
        "plan, because guessing one is how an operator ends up believing "
        "billing is live while everything is free."
    )


def _assert_feature_id(feature_id: str) -> None:
    """Refuse a malformed feature id rather than answering for it.

    A blank or non-string id is a bug at the CALL SITE. Returning the ungated
    ``True`` for it would hide that bug forever, which is exactly the silent
    fallback the house rules ban. This is not the ENT-04 case: an id that is
    simply unknown to the catalog is still answered, and answered "entitled".
    """
    if not isinstance(feature_id, str) or not feature_id.strip():
        raise ValueError(
            f"feature_id must be a non-empty string, got {feature_id!r}. "
            "Pass a stable dotted id (see apps/entitlements/catalog.py); an "
            "entitlement question with no subject has no honest answer."
        )


def configured_provider() -> str | None:
    """The configured payment provider, or None in the shipped inert state.

    Raises rather than returning a name, because a named provider that has no
    implementation behind it is a configuration error, not a state to serve.
    """
    raw = os.environ.get(PROVIDER_ENV, "").strip()
    if not raw:
        return None
    raise EntitlementProviderError(_runbook(raw))


def current_plan() -> Plan:
    """The plan behind this daemon. :data:`UNGATED_PLAN` while inert."""
    configured_provider()
    return UNGATED_PLAN


def has(feature_id: str) -> bool:
    """May the current account use ``feature_id``?

    True for everything while no provider is configured, INCLUDING ids that
    are not in the catalog (ENT-04): an unmapped feature must never read as a
    denial, or adding a call site before its catalog entry would silently
    switch a working feature off.
    """
    _assert_feature_id(feature_id)
    configured_provider()
    return True


def quota(feature_id: str) -> int | None:
    """How many of ``feature_id`` the current account may use.

    None means UNLIMITED, which is every feature while no provider is
    configured. None never means "unknown": an unresolvable quota raises.
    """
    _assert_feature_id(feature_id)
    configured_provider()
    return None


__all__ = [
    "NOT_IN_PLAN_CODE",
    "NOT_IN_PLAN_MESSAGE",
    "PLAN_ID_UNGATED",
    "PLAN_LABEL_UNGATED",
    "PLAN_NOTE_UNGATED",
    "PROVIDER_ENV",
    "UI_REFUSAL_TITLE",
    "UNGATED_PLAN",
    "EntitlementProviderError",
    "Plan",
    "configured_provider",
    "current_plan",
    "has",
    "quota",
]
