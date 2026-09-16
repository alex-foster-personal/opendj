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

from apps.entitlements import lifecycle
from apps.entitlements.source import EntitlementSource, Standing
from apps.shared.wire_codes import NOT_IN_PLAN_CODE

#: The one switch. Unset or blank -> inert. Any value -> fail fast, because
#: no provider integration exists to name.
PROVIDER_ENV: str = "MDT_ENTITLEMENTS_PROVIDER"

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


def _assert_subject(subject: str | None, source: EntitlementSource) -> str:
    """Refuse to consult a source without a subject to consult it about.

    A per-subject source asked about nobody has no honest answer, and every
    guess is wrong one way or the other: "entitled" hands a paid feature to an
    unauthenticated caller, "not entitled" refuses a paying one. So the
    source is never reached with ``subject=None`` -- the caller has a bug to
    fix (it did not resolve the owner) and this says so.
    """
    if subject is None or not isinstance(subject, str) or not subject.strip():
        raise ValueError(
            f"subject must be a non-empty Google sub when a source is given, "
            f"got {subject!r}; source {source.provider!r} was NOT consulted. "
            "Resolve the caller to its owner (machine_owners.google_sub) "
            "before asking what it may do."
        )
    return subject


def standing(feature_id: str, *, subject: str | None, source: EntitlementSource) -> Standing | None:
    """Where ``subject`` stands for ``feature_id`` according to ``source``.

    The lifecycle-aware read behind :func:`has` and :func:`quota`, for a
    server-side check point that must tell ``read_only`` (pull works) apart
    from ``archived`` (nothing works). None means the source holds no record
    for the subject at all.
    """
    _assert_feature_id(feature_id)
    configured_provider()
    return source.standing(_assert_subject(subject, source), feature_id)


def has(
    feature_id: str,
    *,
    subject: str | None = None,
    source: EntitlementSource | None = None,
) -> bool:
    """May the account use ``feature_id``?

    True for everything while no source is given, INCLUDING ids that are not
    in the catalog (ENT-04): an unmapped feature must never read as a
    denial, or adding a call site before its catalog entry would silently
    switch a working feature off. ``subject`` alone changes nothing: without
    a source there is nothing to ask about it.

    With a source, True only for a lifecycle state that admits writes
    (``active``, ``past_due``); a subject the source has no record for is not
    entitled.
    """
    _assert_feature_id(feature_id)
    configured_provider()
    if source is None:
        return True
    found = standing(feature_id, subject=subject, source=source)
    return found is not None and lifecycle.allows(found.state, "write")


def quota(
    feature_id: str,
    *,
    subject: str | None = None,
    source: EntitlementSource | None = None,
) -> int | None:
    """How many of ``feature_id`` the account may use.

    None means UNLIMITED, which is every feature while no source is given.
    None never means "unknown": an unresolvable quota raises. With a source,
    the subject's reported quota, or 0 when the source has no record for it.
    """
    _assert_feature_id(feature_id)
    configured_provider()
    if source is None:
        return None
    found = standing(feature_id, subject=subject, source=source)
    return 0 if found is None else found.quota


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
    "standing",
]
