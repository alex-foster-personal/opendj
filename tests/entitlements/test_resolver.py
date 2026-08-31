"""The entitlement seam: inert, honest, and impossible to misconfigure quietly.

The whole value of this layer is that it ships doing NOTHING while still being
the only place a future gate can live. Both halves need defending, because
both fail silently: a resolver that starts denying things breaks features
nobody sold, and a resolver that keeps saying yes after a provider is
configured lets an operator believe billing is live.

Regression lines:
  - if the default resolver denies ANY feature while no provider is
    configured then a user loses a feature nobody was ever charged for
  - if an unknown feature id reads as denied then adding a call site before
    its catalog entry silently switches a working feature off
  - if quota() reports a number rather than unlimited while inert then a cap
    exists that nobody set
  - if a configured provider is answered instead of refused then the seam is
    guessing a plan
  - if the UI refusal title collides with the PARITY-TODO wording or a
    capability refusal then a dead control lies about why (ENT-03)
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

import pytest

from apps.entitlements import (
    FEATURES,
    NOT_IN_PLAN_CODE,
    NOT_IN_PLAN_MESSAGE,
    PROVIDER_ENV,
    UI_REFUSAL_TITLE,
    EntitlementProviderError,
    configured_provider,
    current_plan,
    has,
    quota,
)
from apps.entitlements.catalog import assert_sellable
from apps.entitlements.resolver import PLAN_ID_UNGATED

#: Ids nothing declares, on purpose: the ENT-04 claim is about UNKNOWN
#: features, so the test has to ask about ones the catalog cannot know.
UNKNOWN_FEATURES: tuple[str, ...] = (
    "cloudsync.hosted-storage",
    "stems.unlimited",
    "anything.at.all",
    "a-plan-name-shaped-string",
)


@pytest.fixture(autouse=True)
def _no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SHIPPED state, asserted rather than inherited from the shell."""
    monkeypatch.delenv(PROVIDER_ENV, raising=False)


# ----- ENT-04: inert and entitled ----------------------------------------
def test_no_provider_is_the_shipped_state() -> None:
    assert configured_provider() is None


@pytest.mark.parametrize("feature_id", UNKNOWN_FEATURES)
def test_unknown_feature_is_entitled_while_no_provider_is_configured(
    feature_id: str,
) -> None:
    assert has(feature_id) is True


@pytest.mark.parametrize("feature_id", UNKNOWN_FEATURES)
def test_unknown_feature_has_no_quota_while_inert(feature_id: str) -> None:
    # None is UNLIMITED here, never "unknown": an unresolvable quota raises.
    assert quota(feature_id) is None


def test_every_catalogued_feature_is_entitled_while_inert() -> None:
    # Vacuous today (the catalog is empty by design) and deliberately written
    # so it stops being vacuous the moment a feature is added.
    for feature in FEATURES:
        assert has(feature.feature_id) is True, feature.feature_id
        assert quota(feature.feature_id) is None, feature.feature_id


def test_plan_says_it_gates_nothing() -> None:
    plan = current_plan()
    assert plan.plan_id == PLAN_ID_UNGATED
    assert plan.provider is None
    # ACCT-02: the plan must not be readable as a free tier of a paid product.
    assert "no paid features" in plan.note


# ----- fail fast on a provider that does not exist ------------------------
@pytest.mark.parametrize("value", ["polar", "stripe", "lemonsqueezy", " x "])
def test_a_configured_provider_refuses_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(PROVIDER_ENV, value)
    calls: tuple[Callable[[], object], ...] = (
        configured_provider,
        current_plan,
        partial(has, "anything"),
        partial(quota, "anything"),
    )
    for call in calls:
        with pytest.raises(EntitlementProviderError) as excinfo:
            call()
        # The house rule is a runbook in the error, not a bare failure.
        assert PROVIDER_ENV in str(excinfo.value)
        assert "RUNBOOK" in str(excinfo.value)


def test_blank_provider_is_the_inert_state_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROVIDER_ENV, "   ")
    assert configured_provider() is None
    assert has("anything") is True


# ----- a malformed question is a bug, not an answer -----------------------
@pytest.mark.parametrize("bad", ["", "   "])
def test_a_blank_feature_id_raises_rather_than_reading_as_entitled(
    bad: str,
) -> None:
    with pytest.raises(ValueError):
        has(bad)
    with pytest.raises(ValueError):
        quota(bad)


# ----- ENT-02 / ENT-03: the refusal shape ---------------------------------
def test_the_refusal_carries_a_stable_code_and_two_distinct_sentences() -> None:
    assert NOT_IN_PLAN_CODE == "entitlement_not_in_plan"
    assert NOT_IN_PLAN_MESSAGE.strip() != ""
    assert UI_REFUSAL_TITLE.strip() != ""
    # The tooltip is short enough to sit in a title attribute; the message is
    # the longer sentence a toast or a body can carry.
    assert len(UI_REFUSAL_TITLE) < len(NOT_IN_PLAN_MESSAGE)


def test_the_plan_refusal_is_a_third_state() -> None:  # ENT-03
    inert_title = "not implemented - see PARITY-TODO"
    from apps.shared.rekordbox_writeback import (
        UI_REFUSAL_TITLE as WRITEBACK_TITLE,
    )

    assert inert_title != UI_REFUSAL_TITLE
    assert WRITEBACK_TITLE != UI_REFUSAL_TITLE
    # Not merely a different string: it must not borrow the OTHER states'
    # vocabulary, which is what makes a user misread which fact they are being
    # told.
    lowered = UI_REFUSAL_TITLE.lower()
    assert "parity" not in lowered
    assert "not implemented" not in lowered
    assert "daemon" not in lowered
    assert "plan" in lowered


# ----- BILL-01: the catalog's admission test ------------------------------
def test_every_catalogued_feature_is_server_side() -> None:
    for feature in FEATURES:
        assert_sellable(feature)


def test_assert_sellable_refuses_a_client_only_feature() -> None:
    from apps.entitlements.catalog import Feature

    with pytest.raises(ValueError) as excinfo:
        assert_sellable(
            Feature(
                feature_id="local.thing",
                label="Local thing",
                server_side=False,
                note="runs entirely on the user's machine",
            )
        )
    assert "server_side=False" in str(excinfo.value)
