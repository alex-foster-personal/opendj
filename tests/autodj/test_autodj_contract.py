"""The AutoDJ contract exposes immutable values and the domain operations its consumers need."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from apps.autodj import choose as choose_module
from apps.autodj import plan as plan_module
from apps.autodj import profile as profile_module
from apps.autodj import render_plan as render_plan_module
from apps.autodj.contract import (
    CONTRACT_VERSION,
    AutoDjService,
    ChooserCandidate,
    KeySegment,
    RenderLayout,
    StyleBars,
    TrackProfile,
    TransitionPlan,
)
from apps.autodj.http import build_service
from apps.autodj.styles import STYLES
from tests.autodj.fixtures import profile


@pytest.mark.requirement("FXTRANS-9850")
def test_list_styles_reports_echo_out_and_tempo_modes() -> None:
    """[if] list_styles runs [then] echo_out and tempo modes match the catalog, [else stop]."""
    by_name = {row.name: row for row in build_service().list_styles()}
    echo = by_name["echo_out"]
    assert echo.available is True and echo.unavailable_reason is None
    assert echo.controls == ("fx_echo", "fx_dry", "play", "master")
    assert echo.spotify == "Echo 1/2 cut end"
    assert echo.bars == StyleBars(min=2, max=5, default=3)
    assert echo.tempo_mode == "none"
    assert by_name["cut"].tempo_mode == "none"
    assert by_name["drop_swap"].tempo_mode == "none"
    for name, style in STYLES.items():
        if style.available and name not in ("cut", "drop_swap", "echo_out"):
            assert by_name[name].tempo_mode == "sync_to_out"


def test_service_satisfies_protocol_and_round_trips_a_proposal() -> None:
    service: AutoDjService = build_service()
    assert isinstance(service, AutoDjService)
    assert CONTRACT_VERSION == "autodj/2"

    out_profile = profile("contract-out")
    in_profile = profile("contract-in", key="9A")
    proposed = service.propose_transition(out_profile, in_profile, out_deck=1, in_deck=2, top=1)
    plan = service.parse_plan(proposed["proposals"][0]["plan"])
    assert service.parse_plan(plan.to_document()) == plan


def test_contract_values_are_strict_frozen_and_keep_planner_identity() -> None:
    service = build_service()
    styles = service.list_styles()
    assert styles and styles[0].name
    with pytest.raises(ValidationError):
        StyleBars(min=1, max=32, default=8, extra=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        styles[0].name = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        KeySegment(start_bar=1, end_bar=8, key="8A", extra=1)  # type: ignore[call-arg]

    value = profile("contract-frozen")
    with pytest.raises(FrozenInstanceError):
        value.title = "changed"  # type: ignore[misc]
    assert profile_module.TrackProfile is TrackProfile
    assert plan_module.TransitionPlan is TransitionPlan
    assert render_plan_module.RenderLayout is RenderLayout
    assert choose_module.Candidate is ChooserCandidate
