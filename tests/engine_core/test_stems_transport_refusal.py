"""The stems plan must say whether this build can actually separate anything.

Without this the setup wizard offered a Separate button on a machine with no
relay base and no identity token, ``POST /api/v1/jobs`` accepted the job, and
the worker died on a missing credential AFTER the wizard had shown "Done".

Regression lines:
- [if] a relay build with no MDT_STEMS_RELAY_URL reports no refusal [then] the
  wizard offers a run that cannot start
- [if] a relay build with a base but no identity token reports no refusal
  [then] the same, one step later
- [if] a fully configured relay build reports a refusal [then] stems are
  switched off for everyone who CAN run them
- [if] an unrecognised MDT_STEMS_TRANSPORT resolves silently [then] a typo
  becomes a fallback instead of an error
- [if] StemsPlanOut stops carrying transport/transport_refusal [then] the
  frontend contract for the honest-inert step is gone

-Claude
"""

from __future__ import annotations

import pytest

from apps.stems.api import StemsPlanOut, stems_transport_state
from apps.stems.job import TRANSPORT_ENV
from apps.stems.relay import contract as relay_api

RELAY_BASE = "https://relay.example.invalid"
TOKEN = "test-identity-token"


@pytest.fixture(autouse=True)
def _clean_transport_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every case states its own environment; nothing leaks in from the shell."""
    monkeypatch.delenv(TRANSPORT_ENV, raising=False)
    monkeypatch.delenv(relay_api.RELAY_BASE_ENV, raising=False)
    monkeypatch.delenv(relay_api.IDENTITY_TOKEN_ENV, raising=False)


def test_a_bare_build_refuses_and_names_the_missing_relay() -> None:
    transport, refusal = stems_transport_state()

    assert transport == "relay"
    assert refusal is not None
    assert relay_api.RELAY_BASE_ENV in refusal


def test_a_relay_base_without_an_identity_token_still_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(relay_api.RELAY_BASE_ENV, RELAY_BASE)

    transport, refusal = stems_transport_state()

    assert transport == "relay"
    assert refusal is not None
    assert relay_api.IDENTITY_TOKEN_ENV in refusal


def test_a_configured_relay_build_does_not_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(relay_api.RELAY_BASE_ENV, RELAY_BASE)
    monkeypatch.setenv(relay_api.IDENTITY_TOKEN_ENV, TOKEN)

    assert stems_transport_state() == ("relay", None)


def test_direct_transport_is_the_operators_own_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'direct' is opt-in, so the credential is the worker's to verify."""
    monkeypatch.setenv(TRANSPORT_ENV, "direct")

    assert stems_transport_state() == ("direct", None)


def test_an_unknown_transport_is_reported_not_silently_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TRANSPORT_ENV, "relayy")

    transport, refusal = stems_transport_state()

    assert transport == "relay"  # the default we fall back to REPORTING as
    assert refusal is not None
    assert "relayy" in refusal


def test_the_plan_model_carries_the_transport_contract() -> None:
    fields = StemsPlanOut.model_fields

    assert "transport" in fields
    assert "transport_refusal" in fields
    assert fields["transport_refusal"].default is None
