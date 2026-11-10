"""#10246 (FXTRANS-11737): the fx_noise plan control, its color_fx NOISE commands, and the end-of-plan check.

[if] fx_noise is validated [then] it compiles to color_fx NOISE and leftover level is refused, [else stop].
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from apps.autodj.compile import _compile_event, noise_command
from apps.autodj.contract.models import PlanError
from apps.autodj.plan import DECK_CONTROLS, FX_CONTROLS, PAGE_PENDING_FX, parse_plan, validate_plan
from apps.autodj.render_plan import TRANSITION_FX_REFUSAL, RenderPlanError, compile_render_plan
from apps.autodj.styles import style
from tests.autodj.test_plan_propose_compile import IN, OUT, _live, _plan

pytestmark = pytest.mark.requirement("FXTRANS-11737")


def _noise_plan(bars: int = 5) -> Any:
    return parse_plan({**_plan("noise_build", bars=bars), "events": style("noise_build").build(bars, 1, 2)})


def _doc(*events: dict[str, Any], bars: int = 5) -> dict[str, Any]:
    return {**_plan("noise_build", bars=bars), "events": list(events)}


def test_fx_noise_is_a_deck_fx_control() -> None:
    """[if] the control sets are read [then] fx_noise is a deck FX control the page is not pending on, [else stop]."""
    assert "fx_noise" in DECK_CONTROLS
    assert "fx_noise" in FX_CONTROLS
    assert "fx_noise" not in PAGE_PENDING_FX


def test_a_set_and_a_ramp_split_into_one_event_per_field() -> None:
    """[if] fx_noise carries level and sweep [then] it parses into a level event and a sweep event, [else stop]."""
    plan = parse_plan(
        _doc(
            {"at_bar": 0, "deck": 1, "set": {"fx_noise": {"level": 0.0, "sweep": 0.0}}},
            {"at_bar": 0, "deck": 1, "ramp": {"fx_noise": {"level": 0.8, "sweep": 0.7}}, "over_bars": 4},
            {"at_bar": 4, "deck": 1, "set": {"fx_noise": {"sweep": -0.5}}},
        )
    )
    noise = [e for e in plan.events if e.name == "fx_noise"]
    assert [(e.kind, e.field, e.value) for e in noise] == [
        ("set", "level", 0.0),
        ("set", "sweep", 0.0),
        ("ramp", "level", 0.8),
        ("ramp", "sweep", 0.7),
        ("set", "sweep", -0.5),
    ]
    assert all(e.over_bars == 4 for e in noise if e.kind == "ramp")


@pytest.mark.parametrize(
    ("event", "match"),
    [
        ({"at_bar": 0, "deck": 1, "set": {"fx_noise": {"level": 1.2}}}, r"fx_noise\.level must be within 0\.\.1"),
        ({"at_bar": 0, "deck": 1, "set": {"fx_noise": {"sweep": 1.5}}}, r"fx_noise\.sweep must be within -1\.\.1"),
        ({"at_bar": 0, "deck": 1, "set": {"fx_noise": {"level": -0.1}}}, r"fx_noise\.level"),
        ({"at_bar": 0, "deck": 1, "set": {"fx_noise": {"color": 0.1}}}, r"'color' is not one of"),
        ({"at_bar": 0, "deck": 1, "set": {"fx_noise": {}}}, r"level and/or sweep"),
        ({"at_bar": 0, "deck": 1, "set": {"fx_noise": 0.5}}, r"level and/or sweep"),
        ({"at_bar": 0, "deck": 1, "set": {"fx_noise": {"level": True}}}, r"fx_noise\.level must be a number"),
        ({"at_bar": 0, "deck": None, "set": {"fx_noise": {"level": 0.5}}}, r"fx_noise needs a deck"),
    ],
)
def test_bad_fx_noise_values_are_refused_naming_the_field(event: dict[str, Any], match: str) -> None:
    """[if] fx_noise carries a bad field or value [then] PlanError names it, [else stop]."""
    with pytest.raises(PlanError, match=match):
        parse_plan(_doc(event))


def test_fx_noise_round_trips_through_json() -> None:
    """[if] a noise_build plan is written and read back [then] it is the same plan, [else stop]."""
    plan = _noise_plan()
    assert parse_plan(plan.to_json()) == plan


def test_fx_noise_compiles_to_color_fx_noise_commands() -> None:
    """[if] fx_noise events compile [then] level is the param and sweep the colour knob, [else stop]."""
    assert noise_command(1, "level", 0.3) == {"type": "color_fx", "deck": 1, "effect": "noise", "param": 0.3}
    assert noise_command(1, "sweep", -1.0)["value"] == 0.0
    with pytest.raises(ValueError, match="no field"):
        noise_command(1, "color", 0.0)
    plan = _noise_plan()
    by_field = {(e.kind, e.field): e for e in plan.events if e.name == "fx_noise" and e.at_bar == 0}
    level_set = _compile_event(by_field["set", "level"])
    assert level_set == {"at_bar": 0, "command": {"type": "color_fx", "deck": 1, "effect": "noise", "param": 0.0}}
    sweep_ramp = _compile_event(by_field["ramp", "sweep"])["ramp"]
    assert sweep_ramp["command"]["value"] == 0.85
    assert sweep_ramp["to"] == 0.85
    level_ramp = _compile_event(by_field["ramp", "level"])["ramp"]
    assert level_ramp["to"] == level_ramp["command"]["param"] == 0.8


def test_noise_build_validates_clean_offline() -> None:
    """[if] noise_build is validated offline [then] it has no errors, [else stop]."""
    assert validate_plan(_noise_plan(), OUT, IN).errors == []


def test_noise_left_on_at_the_end_is_refused() -> None:
    """[if] fx_noise stays on at plan end [then] validation refuses it naming the field, [else stop]."""
    plan = _noise_plan()
    no_cut = replace(plan, events=tuple(e for e in plan.events if not (e.name == "fx_noise" and e.at_bar == 4)))
    errors = validate_plan(no_cut, OUT, IN).errors
    assert any("fx_noise level is 0.8 at the plan's end" in err for err in errors)
    assert any("fx_noise sweep is 0.7 at the plan's end" in err for err in errors)
    half = replace(
        plan,
        events=tuple(
            replace(e, value=0.4) if e.name == "fx_noise" and e.at_bar == 4 and e.field == "level" else e
            for e in plan.events
        ),
    )
    assert any("fx_noise level is 0.4" in err for err in validate_plan(half, OUT, IN).errors)


def test_fx_noise_on_the_incoming_deck_is_refused() -> None:
    """[if] fx_noise runs on the incoming deck [then] validation says FX move only the outgoing deck, [else stop]."""
    plan = _noise_plan()
    wrong = replace(plan, events=tuple(replace(e, deck=2) if e.name == "fx_noise" else e for e in plan.events))
    assert any("only the outgoing deck" in err for err in validate_plan(wrong, OUT, IN).errors)


def test_the_offline_render_refuses_fx_noise_by_name() -> None:
    """[if] noise_build is compiled for render [then] it is refused naming fx_noise and FXTRANS-03, [else stop]."""
    stub = Path(Path.cwd().anchor) / "a.wav"
    with pytest.raises(RenderPlanError, match="fx_noise") as caught:
        compile_render_plan(_noise_plan(), OUT, IN, 4, out_audio=stub, in_audio=stub)
    assert TRANSITION_FX_REFUSAL in str(caught.value)


def test_live_validation_names_only_fx_reset_as_page_pending() -> None:
    """[if] noise_build is validated live [then] fx_reset is refused naming #9848 and fx_noise is not, [else stop]."""
    errors = validate_plan(_noise_plan(), OUT, IN, live=_live()).errors
    assert any("fx_reset" in err and "#9848" in err for err in errors)
    assert not any("fx_noise" in err and "#9848" in err for err in errors)
