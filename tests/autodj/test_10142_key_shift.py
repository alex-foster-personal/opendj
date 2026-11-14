"""#10142: a plan carries the incoming key shift and keylock; the proposer suggests a shift, never applies it."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from apps.autodj.compile import compile_plan
from apps.autodj.contract.models import PlanError, RenderPlanError
from apps.autodj.plan import parse_plan, validate_plan
from apps.autodj.propose import propose_transition
from apps.autodj.render_plan import compile_render_plan
from apps.shared.harmonic import best_key_shift
from tests.autodj.fixtures import profile

OUT, IN = profile("trk-out", key="8A"), profile("trk-in", key="3A")
WAV = Path(Path.cwd().anchor)


def _top() -> dict[str, Any]:
    return propose_transition(OUT, IN, out_deck=1, in_deck=2, top=1)["proposals"][0]


def _doc(**side: Any) -> dict[str, Any]:
    doc = _top()["plan"]
    doc["in"].update(side)
    return doc


def test_key_fields_parse_with_defaults_and_refuse_bad_values_by_name() -> None:
    """[if] in.key_shift_semitones and in.keylock are given [then] -3..3 and bools parse, others refuse by name, [else stop]."""
    plan = parse_plan(_doc())
    assert (plan.in_.key_shift_semitones, plan.in_.keylock) == (0, True)
    shifted = parse_plan(_doc(key_shift_semitones=-1, keylock=False))
    assert (shifted.in_.key_shift_semitones, shifted.in_.keylock) == (-1, False)
    assert parse_plan(shifted.to_json()).in_ == shifted.in_
    for bad in ({"key_shift_semitones": 4}, {"key_shift_semitones": 1.0}, {"key_shift_semitones": True}):
        with pytest.raises(PlanError, match="key_shift_semitones"):
            parse_plan(_doc(**bad))
    with pytest.raises(PlanError, match="keylock"):
        parse_plan(_doc(keylock="yes"))


def test_compile_emits_key_shift_and_keylock_and_render_refuses_by_name() -> None:
    """[if] a plan shifts key with keylock off [then] compile emits key_shift and master_tempo off, render refuses, [else stop]."""
    plan = parse_plan(_doc(key_shift_semitones=-1, keylock=False))
    order = compile_plan(plan, IN, plan_id="p")["plan"]
    assert {"type": "key_shift", "deck": 2, "semitones": -1} in order["prepare"]
    assert order["master_tempo"]["2"] is False
    plain = compile_plan(parse_plan(_doc()), IN, plan_id="p")["plan"]
    assert not any(c["type"] == "key_shift" for c in plain["prepare"])
    with pytest.raises(RenderPlanError, match="static key shift not rendered yet"):
        compile_render_plan(plan, OUT, IN, out_audio=WAV / "a.wav", in_audio=WAV / "b.wav")


def test_best_key_shift_lands_3a_on_8a() -> None:
    """[if] 8A goes to 3A with no vocals [then] best_key_shift returns -1 with gain >= 0.15, same key returns 0, [else stop]."""
    shift, gain = best_key_shift("8A", "3A", keylock_in=True, ratio_in=1.0, vocal_overlap=False)
    assert shift == -1 and gain >= 0.15
    assert best_key_shift("8A", "8A") == (0, 0.0)


def test_propose_suggests_the_shift_without_setting_it() -> None:
    """[if] the pair is 8A to 3A [then] each proposal suggests key -1 in its rationale and leaves the plan at 0, [else stop]."""
    row = _top()
    assert row["suggested_key_shift"]["semitones"] == -1
    assert row["suggested_key_shift"]["from"] == "3A" and row["suggested_key_shift"]["to"] == "8A"
    assert "suggest key -1: 3A lands on 8A" in row["rationale"]
    assert parse_plan(row["plan"]).in_.key_shift_semitones == 0
    same = propose_transition(OUT, profile("trk-in", key="8A"), out_deck=1, in_deck=2, top=1)["proposals"][0]
    assert same["suggested_key_shift"] is None


def test_keylock_off_at_ratio_103_reports_half_a_semitone_detune() -> None:
    """[if] keylock is off and the incoming plays at ratio 1.03 [then] detune 0.49..0.51 is reported and warned, [else stop]."""
    slow = profile("trk-in", bpm=128.0 / 1.03, key="3A")
    doc = propose_transition(OUT, slow, out_deck=1, in_deck=2, top=1)["proposals"][0]["plan"]
    doc["in"]["keylock"] = False
    doc["tempo"] = {"mode": "sync_to_out"}
    plan = parse_plan(doc)
    result = validate_plan(plan, OUT, slow)
    assert 0.49 <= result.metrics["in_detune_semitones"] <= 0.51
    assert "half a semitone out: keep keylock on or shift" in result.warnings
    assert (
        "in_detune_semitones"
        not in validate_plan(replace(plan, in_=replace(plan.in_, keylock=True)), OUT, slow).metrics
    )
