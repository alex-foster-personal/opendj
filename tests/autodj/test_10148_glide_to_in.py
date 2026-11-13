"""#10148: tempo mode glide_to_in, the leader glide that lands the incoming track at its own tempo (spec 04 s3, s5a)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from apps.autodj.compile import compile_plan
from apps.autodj.contract.models import PlanError
from apps.autodj.notation import parse_score, print_score
from apps.autodj.plan import GLIDE_MAX_PCT_PER_BAR, glide_span, handover_bar, parse_plan, validate_plan
from apps.autodj.propose import propose_transition
from apps.autodj.render_plan import compile_render_plan, layout_of
from apps.autodj.styles import STYLES, plan_tempo_mode
from tests.autodj.fixtures import profile

OUT, IN = profile("trk-out", bpm=124.0), profile("trk-in", bpm=128.0, key="9A")
WAV = Path(Path.cwd().anchor)


def _glide_plan(over_bars: int | None = None) -> Any:
    """The top proposed blend between 124 and 128 BPM, which proposes glide_to_in."""
    plans = [parse_plan(p["plan"]) for p in propose_transition(OUT, IN, out_deck=1, in_deck=2, top=6)["proposals"]]
    plan = next(p for p in plans if p.tempo_mode == "glide_to_in")
    return plan if over_bars is None else replace(plan, tempo_over_bars=over_bars)


def _live(in_bpm: float = 128.0, out_pitch: float = 1.0) -> dict[str, Any]:
    return {
        "master_deck": 1,
        "decks": {
            "1": {"effective_bpm": 124.0 * out_pitch, "pitch": out_pitch, "pitch_range": 8, "playing": True},
            "2": {"stable_id": "trk-in", "effective_bpm": in_bpm, "pitch": 1.0, "pitch_range": 8, "playing": False},
        },
    }


def test_glide_parses_and_compiles_to_one_leader_ramp_ending_at_the_handover() -> None:
    """[if] glide_to_in parses [then] it compiles to one leader ramp ending at the handover at in/out BPM, [else stop]."""
    plan = _glide_plan()
    assert plan.tempo_over_bars == plan.bars  # default: the plan's bars
    doc = plan.to_json()
    doc["tempo"] = {"mode": "glide_to_in", "over_bars": 4}
    assert parse_plan(doc).tempo_over_bars == 4
    for bad in (0, plan.bars + 1):
        doc["tempo"]["over_bars"] = bad
        with pytest.raises(PlanError):
            parse_plan(doc)
    order = compile_plan(plan, IN, plan_id="p", live=_live())["plan"]
    ramps = [e for e in order["events"] if "ramp" in e and e["ramp"]["command"]["type"] == "tempo"]
    assert len(ramps) == 1 and ramps[0]["ramp"]["command"] == {"type": "tempo", "deck": 1, "ratio": round(128 / 124, 6)}
    end = ramps[0]["at_bar"] + ramps[0]["ramp"]["over_bars"]
    assert end == handover_bar(plan)
    master = [e for e in order["events"] if e.get("command", {}).get("type") == "master"]
    assert master and master[-1]["command"]["deck"] == 2 and master[-1]["at_bar"] == end


def test_keylock_is_off_near_unity_and_on_for_a_larger_move_with_a_note() -> None:
    """[if] a deck stays within 0.5% of rate 1.0 [then] master_tempo is off, a bigger move keeps it on, noted, [else stop]."""
    plan = _glide_plan()
    order = compile_plan(plan, IN, plan_id="p", live=_live())["plan"]
    assert order["master_tempo"] == {"1": True, "2": True}  # 3.2% moves on both decks
    near = profile("trk-in", bpm=124.5, key="9A")
    synced = replace(plan, tempo_mode="sync_to_out", tempo_over_bars=None)
    near_order = compile_plan(synced, near, plan_id="p", live=_live(124.5))["plan"]
    assert near_order["master_tempo"] == {"1": False, "2": False}
    assert any("deck 2 key lock off" in note for note in near_order["notes"])
    assert any("deck 1 key lock on" in note for note in order["notes"])


def test_validate_warns_on_a_steep_glide_and_a_gap_past_the_reach() -> None:
    """[if] a glide is steeper than GLIDE_MAX_PCT_PER_BAR or past the reach [then] validation warns, never errors, [else stop]."""
    gentle = validate_plan(_glide_plan(), OUT, IN)
    assert not any("glide" in w for w in gentle.warnings)
    steep = validate_plan(_glide_plan(over_bars=2), OUT, IN)
    assert steep.metrics["glide_pct_per_bar"] > GLIDE_MAX_PCT_PER_BAR
    assert any("glide slope" in w for w in steep.warnings)
    assert not any("glide" in e for e in steep.errors)
    far = validate_plan(_glide_plan(), OUT, profile("trk-in", bpm=134.0, key="9A"))
    assert any("beyond the incoming Beat Sync" in w for w in far.warnings)


def test_render_resolves_the_glide_to_per_bar_ramps_ending_with_the_incoming_at_rate_one() -> None:
    """[if] a 124 to 128 glide renders [then] per-bar tempo ramps end with the incoming at 1.0, no beat_sync, [else stop]."""
    plan = _glide_plan()
    rendered = compile_render_plan(plan, OUT, IN, out_audio=WAV / "a.wav", in_audio=WAV / "b.wav")
    assert all(e.get("cmd", {}).get("type") != "beat_sync" for e in rendered["events"])
    ramps = [e["ramp"] for e in rendered["events"] if e.get("ramp", {}).get("type") == "tempo"]
    span = glide_span(plan)
    assert span is not None and len(ramps) == 2 * round(span[1] - span[0])
    in_ramps = [r for r in ramps if r["deck"] == 2]
    assert in_ramps[-1]["to"] == pytest.approx(1.0)
    layout = layout_of(plan, OUT, IN)
    out_last = [r for r in ramps if r["deck"] == 1][-1]["to"]
    assert out_last / in_ramps[-1]["to"] == pytest.approx(layout.out_ratio / layout.in_ratio)


def test_propose_authors_glide_on_blends_sync_on_cuts_and_names_the_glide() -> None:
    """[if] a blend's gap is 0.5% to the reach [then] it glides, cuts sync, unsynced stay none, named, [else stop]."""
    plan = _glide_plan()
    assert plan.rationale is not None and "glide 124 to 128 BPM over" in plan.rationale
    blend = STYLES["long_blend"]
    assert plan_tempo_mode(blend, 16, 3.2, 6.0) == "glide_to_in"
    assert plan_tempo_mode(blend, 16, 0.2, 6.0) == "sync_to_out"
    assert plan_tempo_mode(blend, 16, 7.0, 6.0) == "sync_to_out"
    assert plan_tempo_mode(blend, 1, 3.2, 6.0) == "sync_to_out"
    assert plan_tempo_mode(STYLES["cut"], 1, 3.2, 6.0) == STYLES["cut"].tempo_mode
    unsynced = next(s for s in STYLES.values() if s.tempo_mode == "none")
    assert plan_tempo_mode(unsynced, 16, 3.2, 6.0) == "none"


def test_a_glide_plan_survives_the_mix_score_text_round_trip() -> None:
    """[if] a glide_to_in plan is printed as a mix score and read back [then] mode and over_bars survive, [else stop]."""
    for over_bars in (None, 4):
        plan = _glide_plan(over_bars)
        text = print_score(plan)
        assert f"tempo glide over {plan.tempo_over_bars}" in text.splitlines()
        assert "tempo none" not in text.splitlines()  # the fault: a glide printed as an unsynced plan
        assert parse_score(text).to_json() == plan.to_json()
    plan = _glide_plan()
    bare = parse_score(print_score(plan).replace(f"tempo glide over {plan.bars}", "tempo glide"))
    assert (
        bare.tempo_mode == "glide_to_in" and bare.tempo_over_bars == plan.bars
    )  # the length defaults to the plan's bars
