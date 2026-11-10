"""Transition plans: parse, validate, propose and compile (AGENT-29) on invented tracks."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from apps.autodj.compile import compile_plan
from apps.autodj.contract.models import INTENTS
from apps.autodj.plan import (
    LOAD_TRIM,
    PLAY_LEAD_BARS,
    ChannelStart,
    PlanError,
    PlanEvent,
    _base_rate,
    _beat_sync_fold,
    _clock_pitch,
    _heard,
    _in_bar,
    _incoming_grid_bars,
    _incoming_rate,
    _playing_at,
    _side_gain,
    _tempo_scale,
    bpm_gap_pct,
    pair_beat_sync_fold,
    parse_plan,
    play_start_bar,
    read_live_channels,
    simulate,
    state_at,
    sync_tempo_ratio,
    validate_plan,
)
from apps.autodj.profile import TrackProfile, _key_segments, profile_from_payloads
from apps.autodj.propose import ENERGY_UP, _overlap_bars, _style_fit, parse_intent, propose_transition
from apps.autodj.render_plan import TRANSITION_FX_REFUSAL, RenderPlanError, compile_render_plan
from apps.autodj.scoring import _key_term, score_pair, score_windows
from apps.autodj.styles import STYLES, exit_bar, list_styles, style
from apps.autodj.windows import MixWindow, mix_windows
from tests.autodj.fixtures import CHORUS, DEFAULT_ARRANGEMENT, INTRO, OUTRO, anlz_payload, profile, track_row

OUT, IN = profile("trk-out"), profile("trk-in", key="9A")


def _events(style_name: str, bars: int) -> list[dict[str, Any]]:
    """A style's events over ``bars``. Tempo and handover tests use 8-bar windows, under the gated blends' minimum
    (D11 intro + D12 body fade); for them the same law runs with a body fade that fits, since the law itself is tested
    on its own in tests/autodj/test_bass_hit_gate.py."""
    from apps.autodj.styles import HIT_GATED, MIN_INTRO_BARS, StyleParams, parametric_builder

    chosen = style(style_name)
    if chosen.hit == HIT_GATED and bars < chosen.min_bars:
        short = StyleParams(volume=chosen.volume_lane, min_body_fade_bars=bars - MIN_INTRO_BARS)
        return parametric_builder(short)(bars, 1, 2)
    assert chosen.builder is not None
    return chosen.builder(bars, 1, 2)


def _plan(style_name: str = "bass_swap_blend", bars: int = 15, out_start: int = 113) -> dict[str, Any]:
    return {
        "out": {"deck": 1, "track": "trk-out", "window": {"start_bar": out_start, "bars": bars}},
        "in": {"deck": 2, "track": "trk-in", "window": {"start_bar": 1, "bars": bars}},
        "style": style_name,
        "events": _events(style_name, bars),
        "shape": "lull_then_drop" if style_name == "lull_then_drop" else None,
    }


@pytest.mark.requirement("AGENT-29")
def test_a_window_shorter_than_the_style_minimum_is_not_stretched_to_it() -> None:
    """[if] a window is shorter than the style's minimum [then] the pair gets no overlap, rather than one stretched past its window, [else stop]."""
    track = profile("trk-a")
    out = replace(mix_windows(track, "out")[0], max_bars=32)
    full_in = replace(mix_windows(track, "in")[0], max_bars=32)
    long_blend = STYLES["long_blend"]
    assert _overlap_bars(long_blend, 32, out, replace(full_in, max_bars=4), 10_000) is None
    assert _overlap_bars(long_blend, 32, replace(out, max_bars=4), full_in, 10_000) is None
    assert _overlap_bars(long_blend, 32, out, full_in, 10_000) == 32  # control: both windows fit the style


def _incoming_with_short_intro(bars: int) -> TrackProfile:
    """Intro of ``bars`` then a vocal chorus: mix-in ``max_bars`` is ``bars`` (vocal/peak at bar bars+1)."""
    return profile(
        "trk-in",
        key="9A",
        arrangement=(
            (INTRO, bars, 0.4, 1.0, False),
            (CHORUS, 32, 1.0, 1.0, True),
            (OUTRO, 16, 0.4, 0.8, False),
        ),
    )


def _outgoing_with_expired_top_six() -> TrackProfile:
    """Six early mix-out outros plus one back-half drop so the seventh-ranked out window is past bar 70."""
    return profile(
        "trk-out-expired-six",
        key="8A",
        arrangement=(
            (OUTRO, 8, 0.4, 0.8, False),
            (OUTRO, 8, 0.4, 0.8, False),
            (OUTRO, 8, 0.4, 0.8, False),
            (OUTRO, 8, 0.4, 0.8, False),
            (OUTRO, 8, 0.4, 0.8, False),
            (OUTRO, 8, 0.4, 0.8, False),
            (INTRO, 48, 0.4, 0.2, False),
            (CHORUS, 32, 1.0, 1.0, False),
        ),
    )


def _assert_cut_fallback(result: dict[str, Any], intro_bars: int) -> None:
    assert result["proposals"]
    cuts = [row for row in result["proposals"] if row["style"] == "cut"]
    assert cuts
    assert result["considered"] >= 1
    rationale = cuts[0]["rationale"]
    assert "fallback" in rationale
    assert any(token in rationale for token in ("vocal", "peak", "usable bars"))
    assert result["not_offered"]
    assert any(
        "min_bars" in row["reason"] and f"max_bars={intro_bars}" in row["reason"] for row in result["not_offered"]
    )


@pytest.mark.requirement("AGENT-29")
@pytest.mark.parametrize("intro_bars", [1, 7])
def test_a_short_in_window_still_gets_a_cut_fallback(intro_bars: int) -> None:
    """[if] every preferred blend's min_bars exceeds every in-window max_bars [then] propose_transition still offers a cut and the rationale names the in-window bound that capped it, [else stop]."""
    incoming = _incoming_with_short_intro(intro_bars)
    assert mix_windows(incoming, "in")[0].max_bars == intro_bars
    result = propose_transition(OUT, incoming, out_deck=1, in_deck=2, intent=None)
    _assert_cut_fallback(result, intro_bars)


@pytest.mark.requirement("AGENT-29")
def test_wind_down_on_a_one_bar_in_window_falls_back_to_a_cut() -> None:
    """[if] WIND_DOWN's blends cannot overlap a 1-bar in-window [then] propose_transition still offers a cut, [else stop]."""
    incoming = _incoming_with_short_intro(1)
    result = propose_transition(OUT, incoming, out_deck=1, in_deck=2, intent="wind_down")
    _assert_cut_fallback(result, 1)


@pytest.mark.requirement("AGENT-29")
def test_smooth_on_full_windows_does_not_fall_back_to_a_cut() -> None:
    """[if] a SMOOTH pair has blend-length windows [then] the top proposal is a blend, not a cut fallback, [else stop]."""
    result = propose_transition(OUT, IN, out_deck=1, in_deck=2, intent="smooth")
    assert result["proposals"]
    assert result["proposals"][0]["style"] != "cut"
    assert "fallback" not in result["proposals"][0]["rationale"]


@pytest.mark.requirement("AGENT-29")
def test_an_overlap_skip_is_listed_in_not_offered() -> None:
    """[if] a window pair cannot overlap a style [then] not_offered records the style, min_bars, the window max_bars and the in-window reason, never a silent skip, [else stop]."""
    incoming = _incoming_with_short_intro(1)
    result = propose_transition(OUT, incoming, out_deck=1, in_deck=2, intent=None)
    blend_skips = [row for row in result["not_offered"] if row["style"] in {"long_blend", "filter_sweep"}]
    assert blend_skips
    joined = " ".join(row["reason"] for row in blend_skips)
    assert "long_blend" in joined or "filter_sweep" in joined
    assert "min_bars" in joined
    assert "max_bars=1" in joined
    assert "in-window" in joined


@pytest.mark.requirement("AGENT-29")
def test_propose_considers_later_out_window_when_top_six_are_before_min_out_bar() -> None:
    """[if] the six highest-ranked outgoing windows sit at or before min_out_bar and a later window remains [then] propose_transition still considers that later window (expired windows are dropped before the six-window cap), [else stop]."""
    outgoing = _outgoing_with_expired_top_six()
    top_six = mix_windows(outgoing, "out", limit=6)
    assert [w.start_bar for w in top_six] == [41, 33, 25, 17, 9, 1]
    assert mix_windows(outgoing, "out", limit=7)[-1].start_bar == 97
    result = propose_transition(outgoing, IN, out_deck=1, in_deck=2, intent="smooth", min_out_bar=70)
    assert mix_windows(outgoing, "out", limit=6, min_start_bar=70)[0].start_bar == 97
    assert result["considered"] >= 1
    assert result["proposals"]
    assert all(row["plan"]["out"]["window"]["start_bar"] == 97 for row in result["proposals"])
    assert all(row["validation"]["ok"] for row in result["proposals"])


@pytest.mark.requirement("AGENT-29")
def test_propose_min_out_bar_none_keeps_same_top_six_out_windows() -> None:
    """[if] min_out_bar is None [then] the ranked outgoing windows are unchanged from before the playhead filter, [else stop]."""
    outgoing = _outgoing_with_expired_top_six()
    ranked = mix_windows(outgoing, "out", limit=6)
    assert [w.start_bar for w in ranked] == [41, 33, 25, 17, 9, 1]
    assert mix_windows(outgoing, "out", limit=6, min_start_bar=None) == ranked
    result = propose_transition(outgoing, IN, out_deck=1, in_deck=2, intent="smooth", min_out_bar=None, top=None)
    starts = {row["plan"]["out"]["window"]["start_bar"] for row in result["proposals"]}
    # propose_transition only expands the top three ranked out windows (see out_windows[:3] in propose.py).
    assert starts == {25, 33, 41}
    assert 97 not in starts


def _live(**out: Any) -> dict[str, Any]:
    deck1 = {"stable_id": "trk-out", "playing": True, "position": {"ms": OUT.time_of_bar(100) * 1000}}
    deck1.update(out)
    return {"master_deck": 1, "decks": {"1": deck1, "2": {"stable_id": "trk-in", "playing": False}}}


# ----- catalog --------------------------------------------------------------


def test_catalog_marks_what_the_executor_cannot_do() -> None:
    names = {row["name"]: row for row in list_styles()}
    assert {"cut", "drop_swap", "filter_sweep", "bass_swap_blend", "long_blend", "echo_out"} <= {
        name for name, row in names.items() if row["available"]
    }
    for name in ("stem_swap", "loop_extend"):
        assert names[name]["available"] is False and names[name]["unavailable_reason"]


_GOLDEN_STYLE_EVENTS_SHA256: dict[tuple[str, int], str] = {
    ("cut", 1): "82db87a932c2eb7c2ca71c48e787ef12f5d2f61b6da3658143a1014a5f0f442c",
    ("drop_swap", 1): "82db87a932c2eb7c2ca71c48e787ef12f5d2f61b6da3658143a1014a5f0f442c",
    ("filter_sweep", 12): "ce593efa8ba8aa59ad645cdb82260b2115286a1f61951ccfcce4b19580cc47bf",
    ("filter_sweep", 15): "135eb51499f16da2e426af115fb7300d44a5a52d1fc0cc73629e34f7a371f356",
    ("filter_sweep", 16): "b5eb691fa9cf3198b41fce169df7be83c2c7f0e85130278bb5dd362a7ab7a80f",
    ("filter_sweep", 32): "bdfab8b9fc057f7dcaa0c30344d9022d98103e94735c3c1b0d9ae01e1f27f506",
    ("bass_swap_blend", 12): "a6e4dacc39d951595bcc51a3adc3d80fc88137f4bc1be921369d53f3eebddb17",
    ("bass_swap_blend", 15): "f21997b96de1727c57952278cc8514ace06d747046bb86639057f84a1b379b89",
    ("bass_swap_blend", 16): "fce519ef3f313b5f2d7f6a77f210ffd572d4f6cbd4dfe5026249e1ca0a713929",
    ("bass_swap_blend", 32): "6d0f8a149228f8558b82452c3ae7e81df74c93f30faffdf44ae62885b9ceb6cc",
    ("long_blend", 32): "4a71f8ae72f999e8fd56335bbf9872bddbad56394b18799123b7cbe7a18353cc",
    ("long_blend", 64): "d25f2091dd271c6255f21f083f6ed71616cfd7cf88d851dddf2c41e6e92930aa",
    ("crossfade", 12): "1c11bae4fc986123be02490abc5e9183434d124d1f1dd97be7b1d2be72cddc6a",
    ("crossfade", 15): "75207c4da179afa136db877485e09ece1c7daf8f1cd11dea65e0a54d521bb239",
    ("crossfade", 16): "fe69e24c116af507c7222f3b03f5848da8718e6be0dcfeb30296bb2d51891207",
    ("crossfade", 32): "153ece1cd8782db07b542993512e65c1d37a8ff7c74357c5b864b6896ad20d71",
    ("rise", 12): "8c38a5fdb8e8928449d7ad629e3904bdb9f665dc2f1c95f8d843afd30d20dae6",
    ("rise", 15): "9f1a1097c4560c5ca94d952a7629e029c5da75a1dfcbf5817bda351a3b21eeb3",
    ("rise", 16): "c74a1f4c14d6c762feeb3a7f0f2383211c2221fe1ac358e4273d4e7dc897c832",
    ("rise", 32): "7187424b5b68f57079b092a9423e172283fc94709b77657279c89a7364486580",
    ("wave", 12): "d48916f267b7bb6f980e4cb4c0bef81cf04a950ec7892ec271a2f772c0ad0ecd",
    ("wave", 15): "1d7bdcc1dbffd5b033d74c96f49f71053aaf154a38ea802a303c2f4842364f74",
    ("wave", 16): "8a97a36269c15f4f3a4a0f8ae44b1292b867b9c2b870638c1b8d1af3657c0db7",
    ("wave", 32): "c947fa956fa9814856250309c489f3d85c73a925e4625e41fb38fec86815162f",
    ("melt", 12): "840593867b3fc1718f1f21e3316c225400fdb16134414cb0bba57c8459a56634",
    ("melt", 15): "e8d1d540da651c793b7d05b7b7b5b029467c06654de3a496fe5ada74e1270ca3",
    ("melt", 16): "67b108d47ee50918f6e321cd8778216d3163790468fc6418cf38c344a98e4358",
    ("melt", 32): "4807e1717faa8e59eba506f1d3ad6aba2e29d984bfcb4b7687c2cbe98d4a7f2c",
    ("lull_then_drop", 8): "a918a7dbfb9ca915f87166ae2b49425b836b98e1d23fb1c332388df5203707fd",
    ("lull_then_drop", 15): "7ba475cca8d1faa946832372fbdae734b6d1f65eb092be54e6eca76a26597e32",
    ("lull_then_drop", 16): "7e4e414a542adcd100e22d4c8a8935f434957e07ef63d4aefd8898a9b439a2e6",
    ("lull_then_drop", 32): "15cb848e50f8676e038b230ec54fd86e17a83210231a408326d24ba8d3d8646e",
}


@pytest.mark.requirement("FXTRANS-9850")
def test_no_other_style_changes_its_events() -> None:
    """[if] echo_out is added [then] every other available style's events stay byte-identical, [else stop]."""
    keys = set(_GOLDEN_STYLE_EVENTS_SHA256)
    for name, s in STYLES.items():
        if not s.available or name in ("echo_out", "noise_build"):
            continue
        for bars in sorted({s.clamp_bars(b) for b in (1, 2, 3, 8, 15, 16, 32, 64)}):
            assert (name, bars) in keys
    for (name, bars), expected in _GOLDEN_STYLE_EVENTS_SHA256.items():
        doc = json.dumps(STYLES[name].build(bars, 1, 2), sort_keys=True, separators=(",", ":"))
        got = hashlib.sha256(doc.encode()).hexdigest()
        assert got == expected, f"regenerate golden from origin/main for ({name!r}, {bars})"


@pytest.mark.requirement("FXTRANS-9850")
def test_echo_out_is_available_with_its_spotify_controls_and_bars() -> None:
    """[if] echo_out is catalogued [then] it is available with fx controls and tempo none, [else stop]."""
    chosen = style("echo_out")
    assert chosen.available
    assert chosen.controls == ("fx_echo", "fx_dry", "play", "master")
    assert chosen.spotify == "Echo 1/2 cut end"
    assert chosen.min_bars == 2 and chosen.max_bars == 5 and chosen.default_bars == 3
    assert chosen.needs_beatmatch is False
    assert chosen.tempo_mode == "none"


@pytest.mark.requirement("FXTRANS-9850")
@pytest.mark.parametrize("bars", [2, 3, 5])
def test_echo_out_sends_closes_the_dry_starts_the_incoming_and_resets(bars: int) -> None:
    """[if] echo_out builds a plan [then] fx ramps, dry closes on E, incoming starts and fx_reset runs, [else stop]."""
    exit_e = exit_bar(bars)
    events = style("echo_out").build(bars, 1, 2)
    assert {"at_bar": exit_e - 0.5, "deck": 1, "set": {"fx_echo": 0.0}} in events
    ramp = next(e for e in events if e.get("ramp", {}).get("fx_echo") == 0.7)
    assert ramp["at_bar"] == exit_e - 0.5 and ramp["over_bars"] == 0.5
    assert {"at_bar": exit_e, "deck": 1, "set": {"fx_dry": 0.0}} in events
    assert {"at_bar": exit_e, "deck": 2, "do": "play"} in events
    assert {"at_bar": float(bars), "deck": 1, "do": "fx_reset"} in events
    plan = parse_plan({**_plan("echo_out", bars=bars), "events": events})
    assert validate_plan(plan, OUT, IN).errors == []
    states = simulate(plan)
    heard_before = any(_heard(s)[0] for s in states if s.bar < exit_e)
    heard_after = any(_heard(s)[0] for s in states if s.bar >= exit_e)
    assert heard_before and not heard_after


@pytest.mark.requirement("FXTRANS-9850")
def test_tempo_none_for_cut_styles_and_sync_for_the_rest() -> None:
    """[if] propose_transition runs every intent [then] cut styles carry tempo none and blends sync_to_out, [else stop]."""
    none_styles = frozenset({"cut", "drop_swap", "echo_out", "noise_build"})
    saw_none, saw_sync = False, False
    for intent in INTENTS:
        result = propose_transition(OUT, IN, out_deck=1, in_deck=2, intent=intent, top=None)
        for row in result["proposals"]:
            mode = row["plan"]["tempo"]["mode"]
            if row["style"] in none_styles:
                assert mode == "none"
                saw_none = True
            else:
                assert mode == "sync_to_out"
                saw_sync = True
    assert saw_none and saw_sync


@pytest.mark.requirement("FXTRANS-9850")
def test_a_tempo_gap_pair_is_offered_echo_out_with_the_gap_named() -> None:
    """[if] a 124 BPM track meets a 96 BPM one [then] echo_out is offered with tempo none and the gap named, [else stop]."""
    out_p = profile("trk-out", bpm=124.0)
    in_p = profile("trk-in", bpm=96.0, key="9A")
    result = propose_transition(out_p, in_p, out_deck=1, in_deck=2, top=None)
    echo = [row for row in result["proposals"] if row["style"] == "echo_out"]
    assert echo
    assert echo[0]["plan"]["tempo"] == {"mode": "none"}
    assert echo[0]["validation"]["ok"]
    text = echo[0]["rationale"]
    assert "124" in text and "96" in text and "tempo gap" in text


@pytest.mark.requirement("FXTRANS-9850")
def test_fx_steps_must_reset_and_keep_a_tail() -> None:
    """[if] an echo_out plan omits fx_reset, closes dry late, or puts fx on deck 2 [then] validation refuses it, [else stop]."""
    bars = 3
    doc = {**_plan("echo_out", bars=bars), "events": style("echo_out").build(bars, 1, 2)}
    plan = parse_plan(doc)
    no_reset = replace(plan, events=tuple(e for e in plan.events if e.name != "fx_reset"))
    assert any("fx_reset" in err for err in validate_plan(no_reset, OUT, IN).errors)
    late_dry = replace(
        plan,
        events=tuple(
            replace(e, at_bar=float(bars)) if e.name == "fx_dry" else e for e in plan.events
        ),
    )
    assert any("echo tail" in err for err in validate_plan(late_dry, OUT, IN).errors)
    wrong_deck = replace(
        plan,
        events=tuple(replace(e, deck=2) if e.name == "fx_echo" else e for e in plan.events),
    )
    assert any("only the outgoing deck" in err for err in validate_plan(wrong_deck, OUT, IN).errors)


@pytest.mark.requirement("FXTRANS-9850")
def test_live_validation_refuses_fx_steps_until_the_page_has_them() -> None:
    """[if] echo_out is validated live [then] fx steps are refused naming #9848, [else stop]."""
    plan = parse_plan({**_plan("echo_out", bars=3), "events": style("echo_out").build(3, 1, 2)})
    live_errors = validate_plan(plan, OUT, IN, live=_live()).errors
    assert any("fx_echo" in err and "#9848" in err for err in live_errors)
    assert validate_plan(plan, OUT, IN, live=None).errors == []


@pytest.mark.requirement("FXTRANS-9850")
def test_compile_render_plan_refuses_echo_out_by_name() -> None:
    """[if] echo_out is compiled for render [then] it is refused naming fx_echo and FXTRANS-03, [else stop]."""
    plan = parse_plan({**_plan("echo_out", bars=3), "events": style("echo_out").build(3, 1, 2)})
    stub = Path(Path.cwd().anchor) / "a.wav"
    with pytest.raises(RenderPlanError, match="fx_echo") as caught:
        compile_render_plan(plan, OUT, IN, 4, out_audio=stub, in_audio=stub)
    assert TRANSITION_FX_REFUSAL in str(caught.value)


@pytest.mark.parametrize("name", [name for name, s in STYLES.items() if s.available])
def test_every_available_style_validates_clean_and_hands_over(name: str) -> None:
    bars = max(STYLES[name].min_bars, min(STYLES[name].max_bars, 15))
    plan = parse_plan(_plan(name, bars=bars, out_start=OUT.bars - bars))
    validation = validate_plan(plan, OUT, IN)
    assert validation.errors == []
    assert validation.metrics["silent_bars"] == 0
    last = [event for event in plan.events if event.at_bar == plan.bars]
    assert any(e.kind == "do" and e.name == "master" and e.deck == 2 for e in last)
    assert any(e.kind == "do" and e.name == "stop" and e.deck == 1 for e in last)


# ----- parse ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda doc: doc["events"].append({"at_bar": 1, "deck": 1, "set": {"volume": 1}}), "volume"),
        (lambda doc: doc["events"].append({"at_bar": 1, "deck": 1, "set": {"fader": 2}}), "0..1"),
        (lambda doc: doc["events"].append({"at_bar": 1, "deck": 1, "set": {"crossfader": 0.5}}), "mixer-wide"),
        (lambda doc: doc["events"].append({"at_bar": 1, "deck": None, "set": {"fader": 0.5}}), "needs a deck"),
        (lambda doc: doc["events"].append({"at_bar": 1, "deck": 2, "ramp": {"fader": 1}}), "over_bars"),
        (lambda doc: doc["events"].append({"at_bar": 1, "deck": 2, "set": {"tempo": 5}}), "ratio"),
        (lambda doc: doc["events"].append({"at_bar": 1, "deck": 2, "do": "explode"}), "do must be"),
        (lambda doc: doc["out"].update(deck=7), "deck 1..4"),
        (lambda doc: doc.update(events=[]), "non-empty"),
        (lambda doc: doc.update(tempo={"mode": "warp"}), "tempo.mode"),
        (lambda doc: doc.update(intent=7), "intent must be text"),
        (lambda doc: doc.update(anchor=[]), "anchor must be one of"),
        (lambda doc: doc.update(tempo={"mode": []}), "tempo.mode must be one of"),
        (lambda doc: doc.update(tempo=[]), "tempo must be an object"),
        (lambda doc: doc.update(tempo=False), "tempo must be an object"),
        (lambda doc: doc.update(tempo=0), "tempo must be an object"),
        (lambda doc: doc["events"].append({"at_bar": 1, "deck": 2, "do": []}), "do must be one of"),
        (lambda doc: doc.update(rationale=["because"]), "rationale must be text"),
    ],
)
def test_parse_refuses_malformed_plans(mutate: Any, message: str) -> None:
    document = _plan()
    mutate(document)
    with pytest.raises(PlanError, match=message):
        parse_plan(document)


def test_plan_round_trips_through_json() -> None:
    plan = parse_plan(_plan())
    assert parse_plan(plan.to_json()) == plan


# ----- validate -------------------------------------------------------------


def test_validation_refuses_an_out_window_past_the_last_downbeat() -> None:
    validation = validate_plan(parse_plan(_plan(bars=16)), OUT, IN)
    assert any("last downbeat" in error for error in validation.errors)


def test_validation_requires_the_incoming_play() -> None:
    document = _plan()
    document["events"] = [e for e in document["events"] if not (e.get("do") == "play" and e["deck"] == 2)]
    validation = validate_plan(parse_plan(document), OUT, IN)
    assert any("no play event" in error for error in validation.errors)


def test_validation_counts_silence_as_an_error() -> None:
    document = _plan()
    document["events"].append({"at_bar": 0, "deck": 1, "set": {"fader": 0.0}})
    document["events"] = [e for e in document["events"] if not ("ramp" in e and e["deck"] == 2)]
    validation = validate_plan(parse_plan(document), OUT, IN)
    assert validation.metrics["silent_bars"] > 0
    assert any("neither deck audible" in error for error in validation.errors)


def test_validation_sees_a_gap_between_bar_midpoints() -> None:
    # Outgoing stops at bar 0.6 and incoming starts at 1.4: both midpoints (0.5, 1.5) hear a deck.
    document = _plan()
    document["events"] += [
        {"at_bar": 0.6, "deck": 1, "do": "stop"},
        {"at_bar": 0.6, "deck": 2, "do": "stop"},
        {"at_bar": 1.4, "deck": 2, "do": "play"},
    ]
    validation = validate_plan(parse_plan(document), OUT, IN)
    assert validation.metrics["silent_bars"] >= 2
    assert any("neither deck audible" in error for error in validation.errors)
    # Control: the incoming deck alone pauses and returns; the outgoing deck stays audible through the gap.
    control = _plan()
    control["events"] += [{"at_bar": 0.6, "deck": 2, "do": "stop"}, {"at_bar": 1.4, "deck": 2, "do": "play"}]
    assert validate_plan(parse_plan(control), OUT, IN).metrics["silent_bars"] == 0


def test_silence_sees_a_fader_ramp_cross_the_audible_floor_between_samples() -> None:
    # The outgoing fader falls from 1 to 0 over bar 0; below the audible floor (0.3) from 0.7. The incoming
    # fader is closed until 0.89. The midpoint of [0.5, 0.89] is 0.695, still heard, so only a flip found
    # inside the interval shows the 0.7 to 0.89 silence.
    document = _plan()
    document["events"] = [
        {"at_bar": 0, "deck": 1, "ramp": {"fader": 0.0}, "over_bars": 1},
        {"at_bar": 0, "deck": 2, "do": "play"},
        {"at_bar": 0, "deck": 2, "set": {"fader": 0.0}},
        {"at_bar": 0.89, "deck": 2, "set": {"fader": 1.0}},
    ]
    validation = validate_plan(parse_plan(document), OUT, IN)
    assert validation.metrics["silent_bars"] >= 1
    # Control: the incoming fader opens at once, so the outgoing fader's fall never leaves the room silent.
    control = _plan()
    control["events"] = [
        {"at_bar": 0, "deck": 1, "ramp": {"fader": 0.0}, "over_bars": 1},
        {"at_bar": 0, "deck": 2, "do": "play"},
        {"at_bar": 0, "deck": 2, "set": {"fader": 1.0}},
    ]
    assert validate_plan(parse_plan(control), OUT, IN).metrics["silent_bars"] == 0


def test_simulate_replays_stops_so_an_early_stop_counts_as_silence() -> None:
    document = _plan()
    document["events"] += [{"at_bar": 3, "deck": 1, "do": "stop"}, {"at_bar": 3, "deck": 2, "do": "stop"}]
    plan = parse_plan(document)
    rows = simulate(plan)
    assert [row.out_playing for row in rows[:3]] == [True, True, True]
    assert not any(row.out_playing or row.in_playing for row in rows[3:])
    validation = validate_plan(plan, OUT, IN)
    assert validation.metrics["silent_bars"] == plan.bars - 3
    assert any("neither deck audible" in error for error in validation.errors)


def test_simulate_resumes_a_deck_played_again_after_a_stop() -> None:
    document = _plan()
    document["events"] += [{"at_bar": 3, "deck": 1, "do": "stop"}, {"at_bar": 5, "deck": 1, "do": "play"}]
    rows = simulate(parse_plan(document))
    assert [row.out_playing for row in rows[2:6]] == [True, False, False, True]


def _live_with_crossfader(assign: str, crossfader: float) -> dict[str, Any]:
    live = _live()
    live["mixer"] = {"crossfader": crossfader, "channels": {"1": {"assign": "THRU"}, "2": {"assign": assign}}}
    return live


def test_validation_refuses_an_incoming_deck_on_the_closed_crossfader_side() -> None:
    validation = validate_plan(parse_plan(_plan()), OUT, IN, live=_live_with_crossfader("A", 1.0))
    assert any("would not be heard" in error for error in validation.errors)
    assert validation.ok is False


def test_validation_accepts_an_incoming_deck_on_the_open_crossfader_side() -> None:
    validation = validate_plan(parse_plan(_plan()), OUT, IN, live=_live_with_crossfader("B", 1.0))
    assert not any("would not be heard" in error for error in validation.errors)


def test_a_plan_that_stops_both_decks_on_its_last_bar_is_refused() -> None:
    # Sampled at bar middles, no bar is silent; the stops on the last bar leave the room silent.
    document = _plan()
    document["events"].append({"at_bar": document["out"]["window"]["bars"], "deck": 2, "do": "stop"})
    validation = validate_plan(parse_plan(document), OUT, IN)
    assert validation.metrics["silent_bars"] == 0
    assert any("ends at bar 15 with the incoming deck 2 unheard" in error for error in validation.errors)
    # Control: the same plan without the extra stop ends with deck 2 heard.
    assert validate_plan(parse_plan(_plan()), OUT, IN).errors == []


def _live_with_sides(out_assign: str, in_assign: str, crossfader: float) -> dict[str, Any]:
    live = _live()
    live["mixer"] = {
        "crossfader": crossfader,
        "channels": {"1": {"assign": out_assign}, "2": {"assign": in_assign}},
    }
    return live


def test_a_crossfader_move_that_closes_both_sides_counts_as_silence() -> None:
    # Deck 1 on A, deck 2 on A, crossfader thrown to B at bar 4 (deck 2 is audible from bar 3 as it rises to its
    # pre-hit level): neither deck passes.
    document = _plan()
    document["events"].append({"at_bar": 4, "deck": None, "set": {"crossfader": 1.0}})
    validation = validate_plan(parse_plan(document), OUT, IN, live=_live_with_sides("A", "A", 0.0))
    assert validation.metrics["silent_bars"] == 11
    assert any("neither deck audible" in error for error in validation.errors)
    # Control: the same plan with deck 2 on side B is heard every bar.
    control = validate_plan(parse_plan(document), OUT, IN, live=_live_with_sides("A", "B", 0.0))
    assert control.metrics["silent_bars"] == 0


def test_the_crossfader_starts_where_the_live_mixer_has_it() -> None:
    # Both decks on A with the live crossfader already at B: silent from the first bar.
    validation = validate_plan(parse_plan(_plan()), OUT, IN, live=_live_with_sides("A", "A", 1.0))
    assert validation.metrics["silent_bars"] == 15
    centered = validate_plan(parse_plan(_plan()), OUT, IN, live=_live_with_sides("A", "A", 0.5))
    assert centered.metrics["silent_bars"] == 0


def test_a_crossfader_ramp_is_followed_bar_by_bar_against_the_channel_sides() -> None:
    # Deck 1 on A, deck 2 on B, the crossfader starts at A and ramps to B over the plan:
    # each deck is heard while its side is open, so there is no silence...
    document = _plan()
    document["events"].append({"at_bar": 0, "deck": None, "ramp": {"crossfader": 1.0}, "over_bars": 15})
    swept = validate_plan(parse_plan(document), OUT, IN, live=_live_with_sides("A", "B", 0.0))
    assert swept.metrics["silent_bars"] == 0
    # The incoming side is closed at the start but the plan opens it, so schedule may run it.
    assert swept.errors == []
    # Control: a plan that ends with the incoming side closed is refused, naming the move.
    document["events"].append({"at_bar": 15, "deck": None, "set": {"crossfader": 0.0}})
    shut = validate_plan(parse_plan(document), OUT, IN, live=_live_with_sides("A", "B", 0.0))
    assert any("assigned to side B and the crossfader ends at 0.00" in e for e in shut.errors)
    # ...and the simulation carries the live start and the ramp.
    rows = simulate(parse_plan(document), crossfader=0.0)
    assert rows[0].crossfader == pytest.approx(0.5 / 15)
    assert rows[-1].crossfader == pytest.approx(14.5 / 15)


def test_without_a_live_mixer_the_crossfader_cannot_silence_a_plan() -> None:
    # No assignments are known offline, so both channels count as THRU.
    document = _plan()
    document["events"].append({"at_bar": 2, "deck": None, "set": {"crossfader": 1.0}})
    assert validate_plan(parse_plan(document), OUT, IN).metrics["silent_bars"] == 0


def test_validation_counts_a_vocal_clash_when_both_drops_overlap() -> None:
    # Leave in the out drop (bar 33, vocals) and enter on the in drop (bar 33, vocals).
    document = _plan(bars=16, out_start=33)
    document["in"]["window"]["start_bar"] = 33
    validation = validate_plan(parse_plan(document), OUT, IN)
    assert validation.metrics["vocal_overlap_bars"] > 0
    clean = validate_plan(parse_plan(_plan()), OUT, IN)
    assert clean.metrics["vocal_overlap_bars"] == 0  # control: outro into intro


def test_bass_swap_keeps_one_bassline_but_a_flat_blend_does_not() -> None:
    # Outro (strong bass) into the build (strong enough bass): both basslines exist.
    document = _plan()
    document["in"]["window"]["start_bar"] = 17
    swapped = validate_plan(parse_plan(document), OUT, IN)
    assert swapped.metrics["bass_overlap_bars"] <= 1
    flat = copy.deepcopy(document)
    flat["events"] = [e for e in flat["events"] if "eq_low" not in (e.get("set") or {})]
    flat_result = validate_plan(parse_plan(flat), OUT, IN)
    assert flat_result.metrics["bass_overlap_bars"] > swapped.metrics["bass_overlap_bars"]


def test_tempo_gap_folds_double_time_and_warns_past_beat_sync() -> None:
    assert bpm_gap_pct(128, 64) == pytest.approx(0.0)
    assert bpm_gap_pct(128, 140) == pytest.approx(9.375)
    fast = profile("trk-in", bpm=140.0)
    validation = validate_plan(parse_plan(_plan()), OUT, fast)
    assert any("Beat Sync" in warning for warning in validation.warnings)


def test_live_validation_never_starts_from_silence() -> None:
    plan = parse_plan(_plan())
    assert validate_plan(plan, OUT, IN, live=_live()).errors == []
    stopped = validate_plan(plan, OUT, IN, live=_live(playing=False))
    assert any("a plan continues a playing set" in error for error in stopped.errors)


def test_live_validation_refuses_a_window_already_passed_and_a_live_incoming_deck() -> None:
    plan = parse_plan(_plan())
    late = validate_plan(plan, OUT, IN, live=_live(position={"ms": OUT.time_of_bar(120) * 1000}))
    assert any("already at bar" in error for error in late.errors)
    live = _live()
    live["decks"]["2"]["playing"] = True
    assert any("already playing" in error for error in validate_plan(plan, OUT, IN, live=live).errors)


def test_live_validation_refuses_an_incoming_deck_armed_for_a_launch() -> None:
    plan = parse_plan(_plan())
    armed = _live()
    armed["decks"]["2"]["quantized_launch_armed"] = {"bar": 4.0}
    assert any("armed to launch" in error for error in validate_plan(plan, OUT, IN, live=armed).errors)


def test_simulate_follows_the_ramps() -> None:
    rows = simulate(parse_plan(_plan()))
    # D11: the incoming deck rises to its pre-hit level (-6 dB) while the outgoing deck is untouched until the hit
    # (bar 7 of this 15-bar blend); D12: from the hit the outgoing body fades out.
    assert rows[0].in_fader < 0.1 and rows[6].in_fader == pytest.approx(0.5, abs=0.01)
    assert rows[6].out_fader == 1.0 and rows[-1].out_fader < 0.1


@pytest.mark.requirement("AGENT-29")
@pytest.mark.parametrize(("first_over", "expected"), [(8, 0.75), (2, 0.5)])
def test_a_replacing_ramp_starts_from_the_lane_at_its_own_start(first_over: int, expected: float) -> None:
    """[if] a ramp starts while another runs [then] it moves from the value then, [else stop].

    The page's executor cancels the running ramp and reads the control live,
    so a fader fading out over 8 bars and turned back up at bar 4 is at 0.5
    there and 0.75 at bar 6. Control: a ramp after the first one ended is unchanged."""
    document = _plan()
    document["events"] = [
        {"at_bar": 0, "deck": 1, "ramp": {"fader": 0.0}, "over_bars": first_over},
        {"at_bar": 4, "deck": 1, "ramp": {"fader": 1.0}, "over_bars": 4},
    ]
    assert state_at(parse_plan(document), 6.0, bar=6).out_fader == pytest.approx(expected)


# ----- score and propose ----------------------------------------------------


def test_score_pair_prefers_compatible_keys() -> None:
    near = score_pair(OUT, profile("trk-in", key="9A"))
    far = score_pair(OUT, profile("trk-in", key="2B"))
    assert near.score > far.score
    assert near.key_distance is not None and far.key_distance is not None
    assert near.key_distance < far.key_distance


def test_key_term_reads_the_directional_camelot_rule() -> None:
    """[if] the harmony term read the symmetric distance table [then] 8B -> 7A would score like 5B -> 6A, [else stop].

    The reason names the rule and the interval instead of "N steps apart"."""
    reasons: list[str] = []
    assert _key_term("8A", "9A", reasons) == (1, "adjacent", 0.90)
    assert reasons == ["keys 8A -> 9A adjacent (fifth up)"]
    _, good_relation, good_score = _key_term("5B", "6A", [])
    _, wrong_relation, wrong_score = _key_term("8B", "7A", [])
    assert (good_relation, wrong_relation) == ("diagonal", "wrong_way_diagonal")
    assert good_score is not None and wrong_score is not None and good_score - wrong_score >= 0.30
    unknown: list[str] = []
    assert _key_term("8A", None, unknown) == (None, None, None)
    assert unknown == ["a key is unknown: harmony not scored"]


def test_score_pair_summary_carries_the_key_relation() -> None:
    """[if] the summary dropped the relation [then] the planner could not see a wrong-way diagonal, [else stop]."""
    pair = score_pair(OUT, profile("trk-in", key="9A"))
    assert pair.key_relation == "adjacent"
    assert pair.summary()["key_relation"] == "adjacent"
    assert any(reason == "keys 8A -> 9A adjacent (fifth up)" for reason in pair.reasons), pair.reasons


@pytest.mark.requirement("TRANSAI-10674")
def test_style_fit_penalizes_clash_relations_not_lift_moves() -> None:
    """[if] jaws is long-blended [then] style fit has no clash penalty, [else stop]."""
    long_blend = STYLES["long_blend"]
    jaws = score_pair(profile("trk-out", key="8A"), profile("trk-in", key="3A"))
    boost = score_pair(profile("trk-out", key="8A"), profile("trk-in", key="10A"))
    tritone = score_pair(profile("trk-out", key="8A"), profile("trk-in", key="2A"))
    pay_attention = score_pair(profile("trk-out", key="8A"), profile("trk-in", key="5A"))
    assert _style_fit(long_blend, jaws, "smooth") == 0.0
    assert _style_fit(long_blend, boost, "smooth") == 0.0
    assert _style_fit(long_blend, pay_attention, "smooth") == 0.0
    assert _style_fit(long_blend, tritone, "smooth") == -0.2
    assert _style_fit(long_blend, boost, ENERGY_UP) == pytest.approx(0.05)


@pytest.mark.requirement("TRANSAI-10674")
def test_check_key_fit_records_relation_and_warns_on_jaws() -> None:
    """[if] jaws overlaps long [then] key metrics and lift warning name jaws, [else stop]."""
    out = profile("trk-out", key="8A")
    inn = profile("trk-in", key="3A")
    validation = validate_plan(parse_plan(_plan(bars=16)), out, inn)
    assert validation.metrics["key_distance"] == 5
    assert validation.metrics["key_relation"] == "jaws"
    assert validation.metrics["key_move"] == "lift"
    assert any(w == "keys 8A and 3A: jaws (semitone up); mix it fast" for w in validation.warnings)


@pytest.mark.requirement("TRANSAI-10674")
def test_check_key_fit_warns_wrong_way_diagonal_without_steps_apart() -> None:
    """[if] 8B to 7A is a long blend [then] warning names wrong-way diagonal, [else stop]."""
    out = profile("trk-out", key="8B")
    inn = profile("trk-in", key="7A")
    validation = validate_plan(parse_plan(_plan(bars=16)), out, inn)
    assert validation.metrics["key_relation"] == "wrong_way_diagonal"
    message = "keys 8B and 7A: wrong-way diagonal, no shared chord tones"
    assert any(w == message for w in validation.warnings)
    assert "steps apart" not in " ".join(validation.warnings)


@pytest.mark.requirement("TRANSAI-10674")
def test_propose_energy_boost_under_energy_up_offers_sixteen_bars_without_clash_penalty() -> None:
    """[if] energy_up meets energy_boost [then] a 16-bar style is offered without steps-apart text, [else stop]."""
    out = profile("trk-out", key="8A")
    inn = profile("trk-in", key="10A")
    result = propose_transition(out, inn, out_deck=1, in_deck=2, intent="keep the energy up", min_out_bar=60, top=20)
    sixteen = next(
        row
        for row in result["proposals"]
        if row["style"] == "bass_swap_blend" and row["plan"]["out"]["window"]["bars"] == 16
    )
    assert "steps apart" not in sixteen["rationale"]
    assert "steps apart" not in " ".join(sixteen["validation"]["warnings"])


@pytest.mark.requirement("TRANSAI-10674")
def test_key_validation_warning_names_relation_not_steps_apart() -> None:
    """[if] tritone overlaps long [then] validation names tritone never steps apart, [else stop]."""
    out = profile("trk-out", key="8A")
    inn = profile("trk-in", key="2A")
    validation = validate_plan(parse_plan(_plan(bars=16)), out, inn)
    assert validation.metrics["key_relation"] == "tritone"
    joined = " ".join(validation.warnings)
    assert "tritone" in joined
    assert "steps apart" not in joined


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("keep the energy up", "energy_up"),
        ("wind it down please", "wind_down"),
        ("keep vocals clean", "vocals_clean"),
        ("something flashy", "showy"),
        (None, "smooth"),
        ("upbeat jazz", "smooth"),  # "up" is a word; "upbeat" is not
    ],
)
def test_parse_intent(text: str | None, intent: str) -> None:
    assert parse_intent(text) == intent


def test_propose_returns_valid_ranked_plans_after_the_live_bar() -> None:
    result = propose_transition(OUT, IN, out_deck=1, in_deck=2, intent="smooth", min_out_bar=101)
    proposals = result["proposals"]
    assert proposals and all(row["validation"]["ok"] for row in proposals)
    scores = [row["score"] for row in proposals]
    assert scores == sorted(scores, reverse=True)
    assert all(row["plan"]["out"]["window"]["start_bar"] >= 101 for row in proposals)


def test_propose_intent_changes_the_style() -> None:
    smooth = propose_transition(OUT, IN, out_deck=1, in_deck=2, intent="smooth", min_out_bar=60)
    showy = propose_transition(OUT, IN, out_deck=1, in_deck=2, intent="showy", min_out_bar=60)
    assert smooth["proposals"][0]["style"] != showy["proposals"][0]["style"]
    assert showy["proposals"][0]["style"] in {"drop_swap", "cut"}


# ----- compile --------------------------------------------------------------


def test_compile_keeps_a_same_bar_set_behind_an_earlier_ramp_on_its_control() -> None:
    """A prepare set runs before every timed event, so it may not jump the ramp validation orders first."""
    document = _plan()
    document["events"] += [
        {"at_bar": 0, "deck": 2, "ramp": {"fader": 0.0}, "over_bars": 2.0},
        {"at_bar": 0, "deck": 2, "set": {"fader": 1.0}},
    ]
    order = compile_plan(parse_plan(document), IN, plan_id="plan-test")["plan"]
    assert {"type": "fader", "deck": 2, "value": 1.0} not in order["prepare"]
    assert any(row.get("command") == {"type": "fader", "deck": 2, "value": 1.0} for row in order["events"])
    # Control: a lone set at bar 0 on the same control still runs in prepare.
    lone = _plan()
    lone["events"] = [event for event in lone["events"] if event.get("deck") != 2]
    lone["events"].append({"at_bar": 0, "deck": 2, "set": {"fader": 1.0}})
    prepare = compile_plan(parse_plan(lone), IN, plan_id="plan-test")["plan"]["prepare"]
    assert {"type": "fader", "deck": 2, "value": 1.0} in prepare


def test_compile_turns_beat_sync_off_for_tempo_mode_none() -> None:
    """A fresh or idle deck may hold Beat Sync on, so mode none must switch it off."""
    document = _plan()
    document["tempo"] = {"mode": "none"}
    prepare = compile_plan(parse_plan(document), IN, plan_id="plan-test")["plan"]["prepare"]
    syncs = [c for c in prepare if c["type"] == "beat_sync"]
    assert syncs == [{"type": "beat_sync", "deck": 2, "enabled": False}]
    # Control: the default mode still turns it on, once.
    synced = compile_plan(parse_plan(_plan()), IN, plan_id="plan-test")["plan"]["prepare"]
    assert [c for c in synced if c["type"] == "beat_sync"] == [{"type": "beat_sync", "deck": 2, "enabled": True}]


def test_a_replay_launches_no_earlier_than_the_stop_it_follows() -> None:
    """[if] a deck stops at 4.9 and plays again at 5 [then] the replay stays after the stop, [else stop]."""
    replay = {"type": "play", "deck": 2, "playing": True, "quantize": True}
    stop = {"type": "play", "deck": 2, "playing": False}
    document = _plan()
    document["events"] += [{"at_bar": 4.9, "deck": 2, "do": "stop"}, {"at_bar": 5, "deck": 2, "do": "play"}]
    events = compile_plan(parse_plan(document), IN, plan_id="plan-test")["plan"]["events"]
    stopped = events.index(next(e for e in events if e.get("command") == stop))
    relaunched = [e for e in events if e.get("command") == replay]
    assert relaunched[-1]["at_bar"] == 4.9 and events.index(relaunched[-1]) > stopped
    # Control: with no stop in between, the replay still gets the full lead.
    lone = _plan()
    lone["events"].append({"at_bar": 5, "deck": 2, "do": "play"})
    lead = [
        e
        for e in compile_plan(parse_plan(lone), IN, plan_id="plan-test")["plan"]["events"]
        if e.get("command") == replay
    ]
    assert lead[-1]["at_bar"] == 5 - PLAY_LEAD_BARS


def test_a_fractional_play_starts_on_the_next_master_downbeat() -> None:
    """[if] a play is authored at bar 0.62 [then] the executor dispatches it at 0.37 and it starts on bar 1."""
    assert play_start_bar(0.62, None) == 1.0
    assert play_start_bar(1.0, None) == 1.0
    assert play_start_bar(0.25, None) == 1.0  # its dispatch at bar 0 has passed that downbeat


def test_the_earliest_play_is_clamped_to_the_executor_bound() -> None:
    """[if] the first incoming play is at bar -1 [then] its launch stays at -1, the executor's lower bound."""
    document = _plan()
    document["events"].append({"at_bar": -1, "deck": 2, "do": "play"})
    events = compile_plan(parse_plan(document), IN, plan_id="plan-test")["plan"]["events"]
    launches = [e["at_bar"] for e in events if e.get("command", {}).get("type") == "play"]
    assert -1.0 in launches and min(launches) == -1.0


def test_compile_cues_the_incoming_deck_and_launches_it_quantized_early() -> None:
    plan = parse_plan(_plan())
    order = compile_plan(plan, IN, plan_id="plan-test", live={"master_deck": 2})["plan"]
    assert order["prepare"][0] == {"type": "master", "deck": 1}
    seek = next(c for c in order["prepare"] if c["type"] == "seek")
    assert seek == {"type": "seek", "deck": 2, "position_ms": 0.0, "exact": True}
    assert {"type": "beat_sync", "deck": 2, "enabled": True} in order["prepare"]
    play = next(e for e in order["events"] if e.get("command", {}).get("type") == "play")
    assert play["at_bar"] == -PLAY_LEAD_BARS
    assert play["command"] == {"type": "play", "deck": 2, "playing": True, "quantize": True}
    stop = next(e for e in order["events"] if e.get("command") == {"type": "play", "deck": 1, "playing": False})
    assert stop["at_bar"] == plan.bars
    assert [e["at_bar"] for e in order["events"]] == sorted(e["at_bar"] for e in order["events"])


def test_compile_maps_controls_onto_bus_commands() -> None:
    document = _plan()
    document["events"] += [
        {"at_bar": 2, "deck": None, "ramp": {"crossfader": 0.5}, "over_bars": 2},
        {"at_bar": 2, "deck": 2, "ramp": {"tempo": 1.02}, "over_bars": 4},
        {"at_bar": 3, "deck": 1, "set": {"eq_high": 0.2}},
    ]
    document["tempo"] = {"mode": "none"}  # synced, an incoming tempo move goes to the leader (AGENT-32)
    order = compile_plan(parse_plan(document), IN, plan_id="plan-test")["plan"]
    ramps = [e["ramp"]["command"] for e in order["events"] if "ramp" in e]
    assert {"type": "crossfader", "value": 0.5} in ramps
    assert {"type": "tempo", "deck": 2, "ratio": 1.02} in ramps
    commands = [e["command"] for e in order["events"] if "command" in e]
    assert {"type": "eq", "deck": 1, "band": "high", "value": 0.2} in commands


def test_compile_seeks_to_the_incoming_window_bar() -> None:
    document = copy.deepcopy(_plan())
    document["in"]["window"]["start_bar"] = 17
    order = compile_plan(parse_plan(document), IN, plan_id="plan-test")["plan"]
    seek = next(c for c in order["prepare"] if c["type"] == "seek")
    assert seek["position_ms"] == pytest.approx(IN.time_of_bar(17) * 1000, abs=1)


# ----- trim and tempo reach -----------------------------------------------------


def _live_with_trim(in_trim: float) -> dict[str, Any]:
    live = _live()
    live["mixer"] = {"crossfader": 0.5, "channels": {"1": {"trim": 0.5}, "2": {"trim": in_trim}}}
    return live


@pytest.mark.requirement("AGENT-47")
def test_offline_simulation_starts_both_trims_at_the_load_knob() -> None:
    """[if] live is None [then] both ChannelStart trims are the load knob, [else stop]."""
    channels = read_live_channels(parse_plan(_plan()), None)
    assert channels[0].trim == LOAD_TRIM
    assert channels[1].trim == LOAD_TRIM


def test_read_live_channels_keeps_an_explicit_live_trim() -> None:
    channels = read_live_channels(parse_plan(_plan()), _live_with_trim(0.4))
    assert channels[0].trim == 0.5
    assert channels[1].trim == 0.4


def test_an_incoming_channel_left_at_trim_zero_is_unheard() -> None:
    """Trim scales the channel's gain, so a fader at full on trim 0 is silent."""
    validation = validate_plan(parse_plan(_plan()), OUT, IN, live=_live_with_trim(0.0))
    assert any("incoming deck 2 unheard" in error for error in validation.errors)
    # Control: the same plan with the incoming trim at unity hands over clean.
    control = validate_plan(parse_plan(_plan()), OUT, IN, live=_live_with_trim(0.5))
    assert control.errors == []
    assert control.metrics["silent_bars"] == 0


def test_a_plan_that_restores_the_incoming_trim_is_heard() -> None:
    document = _plan()
    document["events"].append({"at_bar": 0, "deck": 2, "set": {"trim": 0.5}})
    validation = validate_plan(parse_plan(document), OUT, IN, live=_live_with_trim(0.0))
    assert validation.errors == []


def test_an_incoming_launch_on_the_earliest_bar_starts_on_bar_0_with_no_pre_roll() -> None:
    """A play at bar -1 dispatches on a downbeat that has already passed, so it starts on bar 0 like one at -0.25."""
    assert play_start_bar(-1.0, None) == 0.0
    for play_at in (-1, -0.25):  # both start on bar 0, so the grid count is the same and neither is refused
        document = _plan(bars=8)
        document["in"]["window"]["start_bar"] = IN.bars - 7
        document["events"] = [
            {**e, "at_bar": play_at} if e.get("do") == "play" and e.get("deck") == 2 else e for e in document["events"]
        ]
        errors = validate_plan(parse_plan(document), OUT, IN).errors
        assert not any("the incoming track runs out of grid" in e for e in errors), errors


def test_a_stop_authored_after_a_quantized_play_takes_effect_first_when_the_play_is_late() -> None:
    """A play at bar 0.9 would launch at bar 1, but a stop at 0.95 clears that armed launch first."""
    events = [
        PlanEvent(0.9, 1, "do", "play"),
        PlanEvent(0.95, 1, "do", "stop"),
    ]
    assert _playing_at(events, 1, 0.97, True) is False  # the stop takes effect before the play's start
    assert _playing_at(events, 1, 1.0, True) is False  # the stop cleared the launch, so the play never starts the deck


def test_the_incoming_grid_count_skips_a_paused_stretch() -> None:
    """Play at 1, stop at 5, play at 8 in a 15-bar plan: 4 + 7 heard bars, not 14."""
    document = _plan(bars=15)
    document["events"] = [e for e in document["events"] if not (e.get("deck") == 2 and e.get("do"))]
    document["events"] += [
        {"at_bar": 1, "deck": 2, "do": "play"},
        {"at_bar": 5, "deck": 2, "do": "stop"},
        {"at_bar": 8, "deck": 2, "do": "play"},
    ]
    assert _incoming_grid_bars(parse_plan(document), 1.0) == pytest.approx(11.0)
    document["events"] = [e for e in document["events"] if not (e.get("deck") == 2 and e.get("do"))]
    document["events"].append({"at_bar": 1, "deck": 2, "do": "play"})
    assert _incoming_grid_bars(parse_plan(document), 1.0) == pytest.approx(
        14.0
    )  # control: one unbroken run, bar 1 to 15


def test_an_authored_incoming_tempo_counts_its_grid_use() -> None:
    """Eight bars at an incoming tempo of 1.06 consume 8.48 grid bars, not 8: the bound takes the fastest ratio."""
    document = _plan(bars=8)
    document["events"].append({"at_bar": 0, "deck": 2, "set": {"tempo": 1.06}})
    assert _incoming_grid_bars(parse_plan(document), 1.0) == pytest.approx(8.48)
    assert _incoming_grid_bars(parse_plan(_plan(bars=8)), 1.0) == pytest.approx(8.0)  # control: no authored tempo


def test_a_clock_tempo_move_stretches_the_incoming_grid_use() -> None:
    """A clock slowed to 0.8 stretches each clock bar, so the incoming deck spans 1.25 grid bars per clock bar."""
    document = _plan(bars=8)
    document["events"].append({"at_bar": 0, "deck": 1, "set": {"tempo": 0.8}})
    plan = parse_plan(document)
    assert _incoming_grid_bars(plan, 1.0) == pytest.approx(10.0)  # 8 playing bars x 1.25
    assert _in_bar(plan, 8, 1.0) == plan.in_.start_bar + 10
    control = parse_plan(_plan(bars=8))
    assert _incoming_grid_bars(control, 1.0) == pytest.approx(8.0)  # control: no clock tempo move
    assert _in_bar(control, 8, 1.0) == control.in_.start_bar + 8


def test_an_authored_clock_tempo_is_relative_to_the_clock_current_pitch() -> None:
    """A clock at pitch 1.10 (effective 132 from 120) set to 0.90 runs at 120 * 0.90, so the incoming deck spans
    120 / 108 = 1.11 grid bars per clock bar, not the 1.01 that divides the effective tempo by the bare 0.90."""
    document = _plan(bars=8)
    document["tempo"] = {"mode": "none"}
    document["events"].append({"at_bar": 0, "deck": 1, "set": {"tempo": 0.9}})
    plan = parse_plan(document)
    live = {
        "master_deck": 1,
        "decks": {
            "1": {"effective_bpm": 132.0, "pitch": 1.1},
            "2": {"effective_bpm": 120.0, "pitch_range": 8.0},
        },
    }
    base = _base_rate(plan, profile("trk-in", bpm=120.0), live)
    clock_pitch = _clock_pitch(plan, live)
    assert _incoming_grid_bars(plan, base, 1.0, clock_pitch) == pytest.approx(8 * 120.0 / 108.0)
    unpitched: dict[str, Any] = {
        "master_deck": 1,
        "decks": {"1": {"effective_bpm": 132.0}, "2": {"effective_bpm": 120.0, "pitch_range": 8.0}},
    }
    unpitched_grid = _incoming_grid_bars(plan, base, 1.0, _clock_pitch(plan, unpitched))
    assert unpitched_grid == pytest.approx(8 * 120.0 / 132.0 / 0.9)


def test_a_plan_that_never_makes_the_incoming_deck_master_is_refused_as_no_handover() -> None:
    """Without the master move the master stays on a deck the plan stopped, so validation refuses it; the move is the control."""
    document = _plan(bars=8)
    document["events"] = [e for e in document["events"] if not (e.get("deck") == 2 and e.get("do") == "master")]
    errors = validate_plan(parse_plan(document), OUT, IN).errors
    assert any("without making the incoming deck 2 master" in error for error in errors)
    control = validate_plan(parse_plan(_plan(bars=8)), OUT, IN).errors
    assert not any("master" in error for error in control)


def test_a_master_move_back_to_the_outgoing_deck_after_the_handover_is_refused() -> None:
    """The last master move decides the master: one back on the outgoing deck after the incoming promotion leaves it
    stopped as master, so validation refuses it; the plan without that move is the control."""
    document = _plan(bars=8)
    incoming_master = next(e for e in document["events"] if e.get("do") == "master" and e.get("deck") == 2)
    document["events"].append({**incoming_master, "deck": 1})
    errors = validate_plan(parse_plan(document), OUT, IN).errors
    assert any("without making the incoming deck 2 master" in error for error in errors)
    control = validate_plan(parse_plan(_plan(bars=8)), OUT, IN).errors
    assert not any("master" in error for error in control)


def test_a_plan_that_leaves_the_outgoing_deck_playing_is_refused_as_no_handover() -> None:
    """Without the outgoing stop both decks play on past the plan, so validation refuses it; the stop is the control."""
    document = _plan(bars=8)
    document["events"] = [e for e in document["events"] if not (e.get("deck") == 1 and e.get("do") == "stop")]
    errors = validate_plan(parse_plan(document), OUT, IN).errors
    assert any("outgoing deck 1 still heard" in error for error in errors)
    control = validate_plan(parse_plan(_plan(bars=8)), OUT, IN).errors
    assert not any("still heard" in error for error in control)


def test_an_outgoing_fader_the_page_still_hears_is_no_handover() -> None:
    """A fader at 0.29 is under the planner's audible floor but above the page's silence threshold, so the deck still
    plays on: refused. A fader at zero is the control."""
    for level, refused in [(0.29, True), (0.0, False)]:
        document = _plan(bars=8)
        document["events"] = [e for e in document["events"] if not (e.get("deck") == 1 and e.get("do") == "stop")]
        document["events"] = [
            e
            for e in document["events"]
            if not (e.get("deck") == 1 and "fader" in {**e.get("set", {}), **e.get("ramp", {})})
        ]
        # The style's crossfader moves close the outgoing side by the end; keep it open so only the fader decides.
        document["events"] = [e for e in document["events"] if "crossfader" not in e.get("set", {})]
        document["events"].append({"at_bar": 2, "deck": 1, "set": {"fader": level}})
        errors = validate_plan(parse_plan(document), OUT, IN).errors
        assert any("outgoing deck 1 still heard" in error for error in errors) is refused


def test_an_outgoing_channel_the_crossfader_leaves_audible_is_no_handover() -> None:
    """An A-side outgoing channel at crossfader 0.9 keeps about 15.6% gain on the engine's equal-power curve, so the
    deck is still heard and the plan is refused; at 1.0 the side is shut and the plan hands over."""
    assert _side_gain("A", 0.9) == pytest.approx(0.1564, abs=1e-4)  # the engine's cos(x * pi / 2)
    assert _side_gain("B", 0.1) == pytest.approx(0.1564, abs=1e-4)  # side B is cos((1 - x) * pi / 2)
    assert _side_gain("THRU", 0.9) == 1.0
    for crossfader, refused in [(0.9, True), (1.0, False)]:
        document = _plan(bars=8)
        document["events"] = [e for e in document["events"] if not (e.get("deck") == 1 and e.get("do") == "stop")]
        document["events"] = [
            e
            for e in document["events"]
            if not (e.get("deck") == 1 and "fader" in {**e.get("set", {}), **e.get("ramp", {})})
        ]
        document["events"] = [e for e in document["events"] if "crossfader" not in e.get("set", {})]
        document["events"].append({"at_bar": 2, "set": {"crossfader": crossfader}})
        live = _live()
        live["mixer"] = {"channels": {"1": {"assign": "A"}, "2": {"assign": "B"}}}
        errors = validate_plan(parse_plan(document), OUT, IN, live=live).errors
        assert any("outgoing deck 1 still heard" in error for error in errors) is refused


def test_an_outgoing_tempo_outside_the_clock_pitch_range_is_refused_before_scheduling() -> None:
    """A 1.20 tempo on the clock deck's plus or minus 8% range is refused like an incoming one: the page would reject it."""
    live = _live(pitch_range=8.0)
    for target, refused in [(1.20, True), (1.05, False)]:  # control: a target inside the clock's range is not refused
        document = _plan(bars=8)
        document["events"].append({"at_bar": 3, "deck": 1, "set": {"tempo": target}})
        errors = validate_plan(parse_plan(document), OUT, IN, live=live).errors
        assert any("tempo 1.2 at bar 3 is outside deck 1's +-8% pitch range" in error for error in errors) is refused


def test_a_folded_beat_sync_consumes_two_incoming_bars_per_clock_bar() -> None:
    """A 64 BPM clock with a 128 BPM incoming track folds to 2x: the track runs two bars per clock bar."""
    live = {
        "master_deck": 1,
        "decks": {
            "1": {"stable_id": "trk-out", "playing": True, "effective_bpm": 64.0},
            "2": {"stable_id": "trk-in", "playing": False, "pitch_range": 8.0},
        },
    }
    in_128 = profile("trk-in", bpm=128.0)
    assert _beat_sync_fold(parse_plan(_plan(bars=8)), in_128, live) == pytest.approx(2.0)
    assert _beat_sync_fold(parse_plan(_plan(bars=8)), in_128, None) == pytest.approx(1.0)  # control: no live state
    assert _incoming_grid_bars(parse_plan(_plan(bars=8)), 2.0) == pytest.approx(16.0)  # 8 playing bars x fold 2


def test_a_stop_before_the_downbeat_cancels_the_armed_launch_in_the_simulation() -> None:
    """A play at 0.62 is armed for bar 1; a stop at 0.7 clears it, so the deck never starts there."""
    events = [PlanEvent(0.62, 1, "do", "play"), PlanEvent(0.7, 1, "do", "stop")]
    assert _playing_at(events, 1, 1.5, False) is False
    assert _playing_at([PlanEvent(0.62, 1, "do", "play")], 1, 1.5, False) is True  # control: no stop, it starts


def test_an_authored_tempo_moves_only_the_bars_after_it_in_the_clash_mapping() -> None:
    """A tempo of 1.06 set at bar 7 leaves bars 1 to 6 at the base rate: bar 6 is still at grid bar 6."""
    document = _plan(bars=8)
    document["events"].append({"at_bar": 7, "deck": 2, "set": {"tempo": 1.06}})
    plan = parse_plan(document)
    assert _in_bar(plan, 6, 1.0) == plan.in_.start_bar + 6
    assert _in_bar(plan, 8, 1.0) == plan.in_.start_bar + 8  # 7 bars at 1.0, then bar 7 at 1.06: 8.06, floored


def test_an_unsynced_incoming_deck_consumes_its_tempo_ratio_per_clock_bar() -> None:
    """Unsynced, a 130 BPM track against a 100 BPM clock consumes 1.3 grid bars per clock bar."""
    document = _plan(bars=8)
    document["tempo"] = {"mode": "none"}
    live = {
        "master_deck": 1,
        "decks": {
            "1": {"stable_id": "trk-out", "playing": True, "effective_bpm": 100.0},
            "2": {"stable_id": "trk-in", "playing": False, "effective_bpm": 130.0, "pitch_range": 8.0},
        },
    }
    plan = parse_plan(document)
    assert _base_rate(plan, profile("trk-in", bpm=130.0), live) == pytest.approx(1.3)
    assert _base_rate(plan, profile("trk-in", bpm=130.0), None) == pytest.approx(1.0)  # control: no live tempos


def test_a_paused_incoming_deck_does_not_advance_its_clash_bar() -> None:
    """Play at 0, stop at 4, play at 8: at plan bar 10 the deck has heard 4 + 2 bars, not 10."""
    document = _plan(bars=15)
    document["events"] = [e for e in document["events"] if not (e.get("deck") == 2 and e.get("do"))]
    document["events"] += [
        {"at_bar": 0, "deck": 2, "do": "play"},
        {"at_bar": 4, "deck": 2, "do": "stop"},
        {"at_bar": 8, "deck": 2, "do": "play"},
    ]
    plan = parse_plan(document)
    assert _in_bar(plan, 10, 1.0) == plan.in_.start_bar + 6
    assert _in_bar(plan, 10, 2.0) == plan.in_.start_bar + 12  # control: the fold doubles the heard bars


def _live_with_faders(out_fader: float, in_fader: float) -> dict[str, Any]:
    live = _live()
    live["mixer"] = {"crossfader": 0.5, "channels": {"1": {"fader": out_fader}, "2": {"fader": in_fader}}}
    return live


def test_an_outgoing_fader_left_down_makes_the_opening_bars_silent() -> None:
    """The plan never raises deck 1's fader, so a live fader at 0 keeps it unheard."""
    validation = validate_plan(parse_plan(_plan()), OUT, IN, live=_live_with_faders(0.0, 1.0))
    assert validation.metrics["silent_bars"] >= 1
    # Control: the live fader up.
    up = validate_plan(parse_plan(_plan()), OUT, IN, live=_live_with_faders(1.0, 1.0))
    assert up.metrics["silent_bars"] == 0
    # AGENT-35: even a plan that tries to raise it itself cannot (the outgoing fader is capped at the live one).
    document = _plan()
    document["events"].insert(0, {"at_bar": 0, "deck": 1, "set": {"fader": 1.0}})
    raised = validate_plan(parse_plan(document), OUT, IN, live=_live_with_faders(0.0, 1.0))
    assert raised.metrics["silent_bars"] >= 1


def test_an_incoming_fader_the_plan_never_moves_starts_where_the_mixer_has_it() -> None:
    document = _plan()
    document["events"] = [e for e in document["events"] if not (e.get("deck") == 2 and "fader" in str(e))]
    down = validate_plan(parse_plan(document), OUT, IN, live=_live_with_faders(1.0, 0.0))
    assert any("incoming deck 2 unheard" in error for error in down.errors)
    up = validate_plan(parse_plan(document), OUT, IN, live=_live_with_faders(1.0, 1.0))
    assert not any("unheard" in error for error in up.errors)


def test_an_outgoing_low_cut_on_the_mixer_counts_as_cut() -> None:
    """Two choruses with no EQ moves clash on bass, unless deck 1's low is already cut."""
    document = _plan(out_start=49)
    document["in"]["window"]["start_bar"] = 49
    document["events"] = [e for e in document["events"] if "eq_low" not in str(e)]
    live = _live()
    live["mixer"] = {"crossfader": 0.5, "channels": {"1": {"eq_low": 0.5}, "2": {}}}
    flat = validate_plan(parse_plan(document), OUT, IN, live=live)
    live["mixer"]["channels"]["1"]["eq_low"] = 0.0
    cut = validate_plan(parse_plan(document), OUT, IN, live=live)
    assert (flat.metrics["bass_overlap_bars"] > 0, cut.metrics["bass_overlap_bars"]) == (True, 0)


def test_simulate_starts_each_channel_where_it_is_given() -> None:
    from apps.autodj.styles import knob_amp

    rows = simulate(parse_plan(_plan()), channels=(ChannelStart(fader=0.4, low=0.1), ChannelStart(trim=0.3)))
    # Deck 1's fader and low are untouched until the hit at bar 7; deck 2's trim never moves. The body fade's targets
    # are capped at 0.4 (AGENT-35: never louder than the DJ left it).
    assert (rows[0].out_fader, rows[0].out_low, rows[0].in_trim) == (0.4, 0.1, 0.3)
    assert all(r.out_fader <= 0.4 + 1e-9 for r in rows)
    # Control: from a full fader the outgoing deck stays full until the hit (no pre-dip, D11) and the plan must
    # keep the low band at one deck after it (r5: correlated kicks sum in amplitude; 0.05 is the -26 dB knob floor).
    full = simulate(parse_plan(_plan()))
    assert rows[0].out_trim == LOAD_TRIM
    assert all(r.out_fader == 1.0 for r in full if r.bar < 7), "if the outgoing moves before the hit then D11 broke"
    assert all(
        r.out_fader * knob_amp(r.out_low) + r.in_fader * knob_amp(r.in_low) <= 1.0 + 0.05 for r in full if r.bar >= 8
    ), "if the two lows sum above one full deck after the hit then the level law broke"


def _live_tempo(out_bpm: float, range_pct: float | None) -> dict[str, Any]:
    live = _live(effective_bpm=out_bpm)
    if range_pct is not None:
        live["decks"]["2"]["pitch_range"] = range_pct
    return live


def test_a_beatmatch_past_the_incoming_pitch_range_is_refused() -> None:
    # 128 BPM incoming against 145 BPM outgoing needs +13.3%: past +-8, inside +-16.
    refused = validate_plan(parse_plan(_plan()), OUT, IN, live=_live_tempo(145.0, 8))
    assert any("cannot match 145.00 BPM from 128.00 within its +-8% pitch range" in e for e in refused.errors)
    reachable = validate_plan(parse_plan(_plan()), OUT, IN, live=_live_tempo(145.0, 16))
    assert not any("pitch range" in e for e in reachable.errors)
    # Double time folds, as Beat Sync's own candidates do: 256 BPM is 128 doubled.
    folded = validate_plan(parse_plan(_plan()), OUT, IN, live=_live_tempo(256.0, 8))
    assert not any("pitch range" in e for e in folded.errors)
    # A cut does not beatmatch, so the range does not bind it.
    cut = validate_plan(parse_plan(_plan("cut", bars=1)), OUT, IN, live=_live_tempo(145.0, 8))
    assert not any("pitch range" in e for e in cut.errors)
    # An unreported range is said, never passed silently.
    unknown = validate_plan(parse_plan(_plan()), OUT, IN, live=_live_tempo(145.0, None))
    assert any("pitch range or a tempo is unknown" in w for w in unknown.warnings)


def test_compile_sets_the_incoming_tempo_before_its_quantized_launch() -> None:
    """A quantized play skips Beat Sync's tempo match, so prepare sets the tempo first."""
    order = compile_plan(parse_plan(_plan()), IN, plan_id="plan-test", live=_live_tempo(132.0, 8))["plan"]
    tempo = [c for c in order["prepare"] if c["type"] == "tempo"]
    assert tempo == [{"type": "tempo", "deck": 2, "ratio": round(132.0 / 128.0, 6)}]
    kinds = [c["type"] for c in order["prepare"]]
    assert kinds.index("tempo") < kinds.index("beat_sync")
    halved = compile_plan(parse_plan(_plan()), IN, plan_id="plan-test", live=_live_tempo(264.0, 8))["plan"]
    assert [c["ratio"] for c in halved["prepare"] if c["type"] == "tempo"] == [round(132.0 / 128.0, 6)]
    # Controls: mode none leaves the tempo alone, and so does a mirror without the range.
    document = _plan()
    document["tempo"] = {"mode": "none"}
    untouched = compile_plan(parse_plan(document), IN, plan_id="plan-test", live=_live_tempo(132.0, 8))["plan"]
    assert not [c for c in untouched["prepare"] if c["type"] == "tempo"]
    unranged = compile_plan(parse_plan(_plan()), IN, plan_id="plan-test", live=_live_tempo(132.0, None))["plan"]
    assert not [c for c in unranged["prepare"] if c["type"] == "tempo"]


def test_a_huge_integer_is_a_plan_error_not_an_overflow() -> None:
    """[if] a plan number is too large for a float [then] it is refused as a plan error, [else stop]."""
    document = _plan()
    document["events"][0]["at_bar"] = 10**400
    with pytest.raises(PlanError, match="must be finite"):
        parse_plan(document)
    # Control: the same plan with an ordinary bar parses.
    assert parse_plan(_plan())


def test_a_silent_gap_shorter_than_a_grid_cell_still_counts() -> None:
    """[if] both decks stop at bar 0.60 and the incoming deck plays again at 0.62 [then] that 0.02-bar gap is silent, [else stop]."""
    document = _plan()
    # The incoming fader opens at once, so only the stops can leave the room silent.
    document["events"] = [event for event in document["events"] if "ramp" not in event or event["deck"] != 2]
    document["events"] += [
        {"at_bar": 0, "deck": 2, "set": {"fader": 1.0}},
        {"at_bar": 0.6, "deck": 1, "do": "stop"},
        {"at_bar": 0.6, "deck": 2, "do": "stop"},
        {"at_bar": 0.62, "deck": 2, "do": "play"},
    ]
    assert validate_plan(parse_plan(document), OUT, IN).metrics["silent_bars"] >= 1
    # Control: the same outgoing stop with the incoming deck left playing is heard throughout.
    heard = copy.deepcopy(document)
    heard["events"] = [event for event in heard["events"] if event.get("at_bar") != 0.6 or event.get("deck") != 2]
    heard["events"] = [event for event in heard["events"] if event.get("at_bar") != 0.62]
    assert validate_plan(parse_plan(heard), OUT, IN).metrics["silent_bars"] == 0


def test_silence_sees_two_ramps_cross_the_floor_inside_one_grid_cell() -> None:
    """[if] the outgoing fade and the incoming rise each cross the audible floor between grid samples [then] the gap counts, [else stop]."""
    document = _plan()
    document["events"] = [event for event in document["events"] if "ramp" not in event or event["deck"] != 2]
    document["events"] += [
        {"at_bar": 0.6, "deck": 1, "ramp": {"fader": 0.0}, "over_bars": 0.2},
        {"at_bar": 0.6, "deck": 2, "ramp": {"fader": 1.0}, "over_bars": 2.0},
    ]
    assert validate_plan(parse_plan(document), OUT, IN).metrics["silent_bars"] >= 1
    # Control: an incoming rise that opens before the outgoing fade reaches the floor leaves no gap.
    quick = copy.deepcopy(document)
    quick["events"][-1]["over_bars"] = 0.1
    assert validate_plan(parse_plan(quick), OUT, IN).metrics["silent_bars"] == 0


def test_a_second_play_before_the_first_downbeat_replaces_the_first_launch() -> None:
    """Two plays before bar 1 leave one pending launch; a stop before bar 1 clears it, so the deck stays silent."""
    events = [PlanEvent(0.5, 1, "do", "play"), PlanEvent(0.62, 1, "do", "play"), PlanEvent(0.7, 1, "do", "stop")]
    assert _playing_at(events, 1, 1.5, False) is False
    replaced = [PlanEvent(0.5, 1, "do", "play"), PlanEvent(0.62, 1, "do", "play")]
    assert _playing_at(replaced, 1, 1.5, False) is True  # control: the replacement still starts on bar 1


def _live_glide(out_bpm: float = 128.0, out_pitch: float = 1.0) -> dict[str, Any]:
    live = _live(effective_bpm=out_bpm, pitch=out_pitch, pitch_range=8)
    live["decks"]["2"].update(pitch_range=8, effective_bpm=IN.bpm, pitch=1.0)
    return live


def test_compile_keeps_beat_sync_on_and_moves_a_synced_glide_onto_the_leader() -> None:
    """AGENT-32: [if] a synced plan ramps the incoming tempo [then] Beat Sync stays on and the LEADER ramps instead,
    to the ratio that puts the incoming deck where the author asked, so the glide never runs open loop."""
    document = _plan()
    document["events"].append({"at_bar": 3, "deck": 2, "ramp": {"tempo": 1.02}, "over_bars": 2})
    # The leader plays a 125 BPM track at pitch 128/125; the incoming 128 BPM track follows it at fold 1.
    order = compile_plan(parse_plan(document), IN, plan_id="plan-test", live=_live_glide(128.0, 128.0 / 125.0))["plan"]
    assert [c for c in order["prepare"] if c["type"] == "beat_sync"] == [
        {"type": "beat_sync", "deck": 2, "enabled": True}
    ]
    ramps = [e["ramp"]["command"] for e in order["events"] if "ramp" in e and e["ramp"]["command"]["type"] == "tempo"]
    assert IN.bpm is not None
    assert ramps == [{"type": "tempo", "deck": 1, "ratio": round(IN.bpm * 1.02 / 125.0, 6)}]
    # Control: the same move in a plan with Beat Sync off stays on the incoming deck, and Beat Sync is off.
    document["tempo"] = {"mode": "none"}
    unsynced = compile_plan(parse_plan(document), IN, plan_id="plan-test", live=_live_glide())["plan"]
    assert [c for c in unsynced["prepare"] if c["type"] == "beat_sync"] == [
        {"type": "beat_sync", "deck": 2, "enabled": False}
    ]
    assert any(e.get("ramp", {}).get("command", {}).get("deck") == 2 for e in unsynced["events"])


def test_a_synced_glide_without_live_tempos_or_past_the_leader_range_is_refused() -> None:
    """AGENT-32: the leader ratio needs both live tempos; without them, or past the leader's range, compile raises."""
    document = _plan()
    document["events"].append({"at_bar": 0, "deck": 2, "set": {"tempo": 1.02}})
    with pytest.raises(ValueError, match="not in the live mirror"):
        compile_plan(parse_plan(document), IN, plan_id="plan-test")
    far = _plan()
    far["events"].append({"at_bar": 3, "deck": 2, "set": {"tempo": 1.07}})
    with pytest.raises(ValueError, match="outside deck 1's"):
        compile_plan(parse_plan(far), IN, plan_id="plan-test", live=_live_glide(128.0, 128.0 / 125.0))
    # The follower's live tempo is required, and must be its own track's.
    glide = _plan()
    glide["events"].append({"at_bar": 3, "deck": 2, "set": {"tempo": 1.02}})
    blind = _live_glide(128.0, 128.0 / 125.0)
    del blind["decks"]["2"]["effective_bpm"]
    with pytest.raises(ValueError, match="deck 2's effective_bpm is unknown"):
        compile_plan(parse_plan(glide), IN, plan_id="plan-test", live=blind)
    stale = _live_glide(128.0, 128.0 / 125.0)
    stale["decks"]["2"]["effective_bpm"] = 140.0
    with pytest.raises(ValueError, match="stale or holds another track"):
        compile_plan(parse_plan(glide), IN, plan_id="plan-test", live=stale)


def test_an_incoming_master_before_the_handover_is_refused_at_validation() -> None:
    """The incoming deck becomes master only at the terminal handover: an earlier master move fails the plan after prepare."""
    document = _plan(bars=8)
    document["events"].append({"at_bar": 2, "deck": 2, "do": "master"})
    errors = validate_plan(parse_plan(document), OUT, IN).errors
    assert any("incoming deck 2 becomes master at bar 2" in error for error in errors)
    # Control: the same master move at the plan's last bar, the terminal handover, is not flagged.
    control = _plan(bars=8)
    control["events"].append({"at_bar": 8, "deck": 2, "do": "master"})
    assert not any("becomes master" in error for error in validate_plan(parse_plan(control), OUT, IN).errors)


def test_a_non_object_tempo_is_refused_and_an_absent_one_defaults_to_sync() -> None:
    """A malformed tempo never silently enables Beat Sync; only an absent field takes the synced default."""
    bad_tempos: tuple[object, ...] = (False, 0, "", [])
    for bad in bad_tempos:
        document = _plan()
        document["tempo"] = bad
        with pytest.raises(PlanError, match="tempo must be an object"):
            parse_plan(document)
    document = _plan()
    document.pop("tempo", None)
    assert parse_plan(document).tempo_mode == "sync_to_out"


def test_key_segments_are_read_as_one_based_inclusive_bars() -> None:
    """The analysis lane counts bars from zero and half-open: [0, 8) is profile bars 1 through 8."""
    anlz = {
        "key_segments": {
            "status": "ok",
            "segments": [{"start_bar": 0, "end_bar": 8, "key_camelot": "8A", "confidence": 0.9}],
        }
    }
    (segment,) = _key_segments(anlz)
    assert (segment["start_bar"], segment["end_bar"], segment["key"]) == (1, 8, "8A")


def test_an_explicit_overlap_longer_than_the_windows_scores_the_bars_they_have() -> None:
    """Bars past either window are not heard, so a 100-bar overlap over 8-bar windows scores as the 8 bars they have."""
    out_window = MixWindow("out", 9, 8, "outro", 0.5, None, None, 0.5, "x")
    in_window = MixWindow("in", 1, 8, "intro", 0.5, None, None, 0.5, "x")
    capped = score_windows(OUT, IN, out_window, in_window, overlap_bars=100)
    assert capped.score == score_windows(OUT, IN, out_window, in_window, overlap_bars=8).score


def test_a_pair_fold_is_the_fold_beat_sync_picks_within_its_reach() -> None:
    """Double time folds to 2 incoming bars per clock bar, half time to 0.5, and a tempo outside the reach stays 1."""
    assert pair_beat_sync_fold(120.0, 240.0) == 2.0
    assert pair_beat_sync_fold(120.0, 60.0) == 0.5
    assert pair_beat_sync_fold(120.0, 121.0) == 1.0
    assert pair_beat_sync_fold(120.0, 100.0) == 1.0
    assert pair_beat_sync_fold(None, 120.0) == 1.0


def test_a_clock_stop_before_the_plan_ends_is_refused_at_validation() -> None:
    """The clock must run to the handover: a stop before the last bar would freeze the executor's clock mid-plan."""
    document = _plan(bars=8)
    document["events"].append({"at_bar": 3, "deck": 1, "do": "stop"})
    errors = validate_plan(parse_plan(document), OUT, IN).errors
    assert any("stops at bar 3, before the plan ends at 8" in error for error in errors)
    # Control: the handover stop at the plan's last bar is not flagged.
    control = validate_plan(parse_plan(_plan(bars=8)), OUT, IN).errors
    assert not any("before the plan ends" in error for error in control)


def test_an_authored_tempo_is_an_absolute_ratio_converted_by_the_tracks_tempos() -> None:
    """Unsynced 130 BPM at 109.2 (0.91 of a 120 clock) set to tempo 1.16 runs at 130 * 1.16 / 120, not 0.91 * 1.16."""
    document = _plan(bars=8)
    document["tempo"] = {"mode": "none"}
    document["events"].append({"at_bar": 0, "deck": 2, "set": {"tempo": 1.16}})
    plan = parse_plan(document)
    in_profile = profile("trk-in", bpm=130.0)
    live = {
        "master_deck": 1,
        "decks": {"1": {"effective_bpm": 120.0}, "2": {"effective_bpm": 109.2, "pitch_range": 8.0}},
    }
    base = _base_rate(plan, in_profile, live)
    scale = _tempo_scale(plan, in_profile, live)
    assert base == pytest.approx(0.91)
    assert _incoming_rate(plan, base, scale) == pytest.approx(130 * 1.16 / 120)
    assert _in_bar(plan, 8, base, scale) == plan.in_.start_bar + int(8 * 130 * 1.16 / 120)  # control: the bar counts it


def test_an_authored_tempo_outside_the_incoming_pitch_range_is_refused_before_scheduling() -> None:
    """A 1.20 tempo on a plus or minus 6% deck is one setTempoRatio refuses mid-plan, so validation refuses it first."""
    live = _live()
    live["decks"]["2"]["pitch_range"] = 6.0
    for target, refused in [(1.20, True), (1.05, False)]:  # control: a target inside the range is not refused
        document = _plan(bars=8)
        document["events"].append({"at_bar": 3, "deck": 2, "set": {"tempo": target}})
        errors = validate_plan(parse_plan(document), OUT, IN, live=live).errors
        assert any("outside deck 2's +-6% pitch range" in error for error in errors) is refused


def test_a_tempo_object_must_name_its_mode() -> None:
    """Only an absent tempo field takes the synced default: an object without mode is refused, never defaulted."""
    from apps.autodj.plan import _tempo_mode

    assert _tempo_mode({}) == "sync_to_out"
    with pytest.raises(PlanError):
        _tempo_mode({"tempo": {}})


@pytest.mark.requirement("AGENT-29")
def test_an_authored_incoming_tempo_ramp_starts_from_the_decks_live_pitch() -> None:
    """Codex/Sol (PR #5822, P1): the executor starts an authored tempo ramp from the deck's live pitch, so validation must seed it there too.

    [if] validation seeds the ramp from 1.0 [then] a non-unity incoming pitch makes the grid it counts differ from the audio played, [else stop].
    """
    document = _plan(bars=15)
    document["events"].append({"at_bar": 0, "ramp": {"tempo": 1.2}, "deck": 2, "over_bars": 4})
    plan = parse_plan(document)
    base, scale, clock_pitch = 1.0, 1.0, 1.0

    def in_bars(in_pitch: float) -> list[int]:
        return [_in_bar(plan, bar, base, scale, clock_pitch, in_pitch) for bar in range(1, 15)]

    assert in_bars(1.25) != in_bars(1.0)


# ----- AGENT-33: unscored terms ----------------------------------------------


def test_an_unknown_key_is_unscored_not_a_hidden_half() -> None:
    """AGENT-33: [if] a key is unknown [then] harmony is unscored with its reason, the score is the mean of the
    measured terms only, and measured reads 3 of 4."""
    out_window = mix_windows(OUT, "out", limit=1)[0]
    in_window = mix_windows(IN, "in", limit=1)[0]
    unknown = score_windows(OUT, profile("trk-in", key=""), out_window, in_window)
    assert unknown.key_distance is None
    assert ("harmony", "a key is unknown") in unknown.unscored
    assert unknown.summary()["measured"] == f"{unknown.measured} of 4"
    assert unknown.summary()["unscored"][0] == {"term": "harmony", "reason": "a key is unknown"}
    # Changing what a hidden 0.5 would have weighed cannot move the score: no value stands in for harmony.
    from apps.autodj import scoring

    original = dict(scoring.TERM_WEIGHTS)
    try:
        scoring.TERM_WEIGHTS["harmony"] = 5.0
        assert score_windows(OUT, profile("trk-in", key=""), out_window, in_window).score == pytest.approx(
            unknown.score
        )
    finally:
        scoring.TERM_WEIGHTS.clear()
        scoring.TERM_WEIGHTS.update(original)
    # Control: a known key IS scored, so it has fewer unscored terms.
    known = score_windows(OUT, IN, out_window, in_window)
    assert "harmony" not in dict(known.unscored) and known.measured == unknown.measured + 1


def test_ranking_puts_more_measured_terms_first() -> None:
    """AGENT-33: a pair missing a term never outranks one measured on more terms, whatever its partial score."""
    out_window = mix_windows(OUT, "out", limit=1)[0]
    in_window = mix_windows(IN, "in", limit=1)[0]
    from apps.autodj.scoring import rank_key

    partial = score_windows(OUT, profile("trk-in", key=""), out_window, in_window)
    full = score_windows(OUT, profile("trk-in", key="2B"), out_window, in_window)
    assert sorted([partial, full], key=rank_key, reverse=True)[0] is full
    assert rank_key(partial, 9.0) < rank_key(full, 0.0)


# ----- AGENT-31: loudness-match trim ------------------------------------------


def _loud(stable_id: str, lufs: float | None) -> TrackProfile:
    row = track_row(stable_id, bars=sum(r[1] for r in DEFAULT_ARRANGEMENT))
    if lufs is not None:
        row["loudness"] = {"lufs": lufs, "status": "ok", "reason": None}
    else:
        row["loudness"] = {"lufs": None, "status": "missing", "reason": "loudness_lufs not analyzed"}
    return profile_from_payloads(anlz_payload(stable_id), row)


def test_the_trim_matches_the_incoming_track_to_the_outgoing_lufs() -> None:
    """AGENT-31: [if] both LUFS are known [then] trim_db is out minus in, proposals carry it, and compile sets the
    incoming TRIM knob to that gain in prepare (-6 dB is knob 0.25 on the linear 2x trim)."""
    from apps.autodj.profile import match_trim_db, trim_knob

    out_p, in_p = _loud("trk-out", -9.0), _loud("trk-in", -3.0)
    assert match_trim_db(out_p, in_p) == (-6.0, "match -3.0 to -9.0 LUFS: -6.0 dB")
    assert trim_knob(-6.0) == pytest.approx(10 ** (-6 / 20) / 2, abs=1e-6)
    proposals = propose_transition(out_p, in_p, out_deck=1, in_deck=2, intent="smooth")["proposals"]
    # Offline, the outgoing deck is assumed loaded at -6 dB, so the incoming matches that effective level.
    assert proposals and all(row["plan"]["in"]["trim_db"] == -12.0 for row in proposals)
    order = compile_plan(parse_plan(proposals[0]["plan"]), in_p, plan_id="p", live=None)["plan"]
    assert {"type": "trim", "deck": 2, "value": trim_knob(-12.0)} in order["prepare"]
    # A quiet incoming track past the knob's top is capped, and the reason says so.
    capped, why = match_trim_db(_loud("trk-out", -6.0), _loud("trk-in", -20.0))
    assert capped == 6.02 and "capped" in why


def test_a_missing_lufs_is_no_trim_with_its_reason_never_a_default() -> None:
    """AGENT-31: [if] a LUFS is unknown [then] trim_db is null, the reason names the track, and compile sends no trim."""
    from apps.autodj.profile import match_trim_db

    out_p, in_p = _loud("trk-out", -9.0), _loud("trk-in", None)
    trim_db, reason = match_trim_db(out_p, in_p)
    assert trim_db is None and "trk-in loudness unknown (loudness_lufs not analyzed)" in reason
    assert in_p.summary()["loudness"] == {"lufs": None, "reason": "loudness_lufs not analyzed"}
    proposals = propose_transition(out_p, in_p, out_deck=1, in_deck=2, intent="smooth")["proposals"]
    assert all(row["plan"]["in"]["trim_db"] is None and row["plan"]["in"]["trim_reason"] == reason for row in proposals)
    order = compile_plan(parse_plan(proposals[0]["plan"]), in_p, plan_id="p")["plan"]
    assert not [c for c in order["prepare"] if c["type"] == "trim"]


def test_trim_db_is_refused_on_the_outgoing_side_and_when_not_a_number() -> None:
    document = _plan()
    document["out"]["trim_db"] = -3.0
    with pytest.raises(PlanError, match="only the incoming side"):
        parse_plan(document)
    document = _plan()
    document["in"]["trim_db"] = "loud"
    with pytest.raises(PlanError, match="trim_db must be a number"):
        parse_plan(document)


# ----- AGENT-34: parametric styles -------------------------------------------


def _fader_at(events: list[dict[str, Any]], deck: int, bar: float) -> float:
    """The fader a ramp chain reaches by ``bar``: the last ramp ending at or before it."""
    ends = [
        (e["at_bar"] + e["over_bars"], e["ramp"]["fader"])
        for e in events
        if e.get("deck") == deck and "fader" in e.get("ramp", {})
    ]
    return max((end for end in ends if end[0] <= bar + 1e-9), key=lambda end: end[0])[1]


def test_the_spotify_set_is_parameter_points_and_stems_stay_unavailable() -> None:
    docs = {row["name"]: row for row in list_styles()}
    assert {docs[n]["spotify"] for n in ("cut", "crossfade", "rise", "wave", "melt")} == {
        "Slam",
        "Fade",
        "Rise",
        "Wave",
        "Melt",
    }
    assert docs["wave"]["params"]["filter"] == "lp_both" and docs["melt"]["params"]["filter"] == "hp_both"
    assert docs["stem_swap"]["available"] is False and "stems" in docs["stem_swap"]["unavailable_reason"]
    assert docs["stem_swap"]["params"]["stems"] == "vocals_swap"
    with pytest.raises(ValueError, match="not available"):
        style("stem_swap").build(16, 1, 2)


def test_validation_knows_the_incoming_trim() -> None:
    """AGENT-31: [if] in.trim_db is past the knob's +6.02 dB [then] validation fails; an authored incoming trim
    beside it, or one moving after the incoming fader opens, fails too; a reachable trim_db validates clean."""

    def with_trim(trim_db: float | None, *extra: dict[str, Any]) -> list[str]:
        document = _plan()
        document["in"]["trim_db"] = trim_db
        document["events"] += list(extra)
        return validate_plan(parse_plan(document), OUT, IN).errors

    assert with_trim(-3.0) == []  # control: a reachable trim passes
    assert any("past the trim knob" in error for error in with_trim(9.0))
    assert any("keep one" in error for error in with_trim(-3.0, {"at_bar": 0, "deck": 2, "set": {"trim": 0.4}}))
    late = with_trim(None, {"at_bar": 6, "deck": 2, "set": {"trim": 0.4}})
    assert any("lands while deck 2 is heard" in error for error in late)


def test_trim_order_is_replayed_in_event_order_from_the_channel_start() -> None:
    """AGENT-31 (Sol P1): same-bar order decides, and a channel that starts open needs no fader event to be heard."""

    def errors(events: list[dict[str, Any]], live: dict[str, Any] | None = None) -> list[str]:
        document = _plan()
        document["events"] = events + [
            e for e in document["events"] if e.get("deck") != 2 or ("do" in e and e["do"] == "master")
        ]
        return [e for e in validate_plan(parse_plan(document), OUT, IN, live=live).errors if "trim" in e]

    play = {"at_bar": 0, "deck": 2, "do": "play"}
    up = {"at_bar": 0, "deck": 2, "ramp": {"fader": 1.0}, "over_bars": 4}
    kill = {"at_bar": 0, "deck": 2, "set": {"fader": 0.0}}
    trim = {"at_bar": 0, "deck": 2, "set": {"trim": 0.4}}
    assert errors([kill, trim, play, up]) == []  # trim before the fader opens on the same bar: safe
    assert errors([kill, play, up, trim]) == []  # a bar-0 trim set is hoisted into prepare, ahead of the play
    assert errors([kill, play, up, {"at_bar": 0.5, "deck": 2, "set": {"trim": 0.4}}])  # timed, after the fader opens
    assert errors([play, {"at_bar": 2, "deck": 2, "set": {"trim": 0.4}}])  # no fader event: the channel starts open


def test_a_deep_trim_is_replayed_from_prepare_and_can_silence_the_incoming_deck() -> None:
    """AGENT-31: the simulation starts the incoming trim where prepare puts it, so a trim that shuts the channel
    shows up as an unheard incoming deck at the end (refused), where the same plan without it passes."""
    document = _plan()
    document["in"]["trim_db"] = -200.0
    errors = validate_plan(parse_plan(document), OUT, IN).errors
    assert errors, "a -200 dB trim leaves the incoming deck unheard"
    assert validate_plan(parse_plan(_plan()), OUT, IN).errors == []


def test_a_trim_after_a_finished_closing_fader_ramp_is_not_heard() -> None:
    """AGENT-31 (Sol P1): a fader ramp toward closed is heard only until it ends; a trim after its end bar is allowed,
    one during it is not."""

    def trim_errors(trim_bar: float) -> list[str]:
        document = _plan()
        document["events"] = [
            {"at_bar": 0, "deck": 2, "do": "play"},
            {"at_bar": 1, "deck": 2, "ramp": {"fader": 0.0}, "over_bars": 2},
            {"at_bar": trim_bar, "deck": 2, "set": {"trim": 0.4}},
            {"at_bar": 4, "deck": 2, "set": {"fader": 1.0}},
            *[e for e in document["events"] if e.get("deck") != 2 or e.get("do") == "master"],
        ]
        return [e for e in validate_plan(parse_plan(document), OUT, IN).errors if "trim" in e]

    assert trim_errors(3.5) == []  # after the close ended at bar 3
    assert trim_errors(2.0)  # mid-ramp: still heard


def test_trim_order_is_judged_after_prepare_hoisting() -> None:
    """AGENT-31 (Sol P1): a same-bar incoming trim set that compile hoists into prepare runs before the timed play,
    so validation judges that order: authored after the play and fader, it still passes. Control: a trim the compiler
    cannot hoist (behind an earlier trim on its control) is still judged where it sits."""

    def trim_errors(events: list[dict[str, Any]]) -> list[str]:
        document = _plan()
        document["events"] = events + [e for e in document["events"] if e.get("deck") != 2 or e.get("do") == "master"]
        return [e for e in validate_plan(parse_plan(document), OUT, IN).errors if "trim" in e]

    play = {"at_bar": 0, "deck": 2, "do": "play"}
    fader = {"at_bar": 0, "deck": 2, "set": {"fader": 1.0}}
    trim = {"at_bar": 0, "deck": 2, "set": {"trim": 0.4}}
    order = compile_plan(
        parse_plan({**_plan(), "events": [play, fader, trim, *_plan()["events"][-2:]]}), IN, plan_id="p"
    )
    assert {"type": "trim", "deck": 2, "value": 0.4} in order["plan"]["prepare"]  # compile hoists it
    assert trim_errors([play, fader, trim]) == []
    earlier = {"at_bar": -0.5, "deck": 2, "ramp": {"trim": 0.5}, "over_bars": 0.25}
    assert trim_errors([earlier, play, fader, trim])  # not hoistable: heard after play and fader


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True])
def test_a_synced_glide_refuses_non_finite_or_boolean_live_tempos(bad: object) -> None:
    """AGENT-32 (Sol P1): NaN, infinity or a boolean in the live tempo is refused, never computed with."""
    document = _plan()
    document["events"].append({"at_bar": 3, "deck": 2, "set": {"tempo": 1.02}})
    live = _live_glide(128.0, 128.0 / 125.0)
    live["decks"]["2"]["effective_bpm"] = bad
    with pytest.raises(ValueError, match="deck 2's effective_bpm is unknown"):
        compile_plan(parse_plan(document), IN, plan_id="plan-test", live=live)


@pytest.mark.parametrize(
    "block",
    [
        {"lufs": "loud", "status": "ok", "reason": None},
        {"lufs": -9.0, "status": "weird", "reason": None},
        {"lufs": None, "status": "missing"},
        {"lufs": None, "status": "failed", "reason": None},
        "not a block",
    ],
)
def test_a_malformed_loudness_block_is_an_error_not_unmeasured(block: object) -> None:
    """AGENT-31 (Sol P1): wrong types, missing keys or an unknown status in the track read's loudness raise."""
    from apps.autodj.profile import ProfileError

    row = track_row("trk-in", bars=sum(r[1] for r in DEFAULT_ARRANGEMENT))
    row["loudness"] = block
    with pytest.raises(ProfileError, match="malformed loudness block"):
        profile_from_payloads(anlz_payload("trk-in"), row)


def test_a_broken_loudness_store_refuses_the_profile() -> None:
    """AGENT-31: status error (no store, wrong schema) is not "unmeasured": the profile refuses it."""
    from apps.autodj.profile import ProfileError

    row = track_row("trk-in", bars=sum(r[1] for r in DEFAULT_ARRANGEMENT))
    row["loudness"] = {"lufs": None, "status": "error", "reason": "loudness store missing: no state.db at /x"}
    with pytest.raises(ProfileError, match="loudness store is broken"):
        profile_from_payloads(anlz_payload("trk-in"), row)


def test_a_non_text_trim_reason_is_refused_not_dropped() -> None:
    document = _plan()
    document["in"]["trim_reason"] = 42
    with pytest.raises(PlanError, match="trim_reason must be text"):
        parse_plan(document)


# ----- AGENT-34: the parameter points as candidates; AGENT-35: a live outgoing fader -----


def test_propose_offers_the_new_parameter_points_as_candidates() -> None:
    styles = {
        row["style"]
        for row in propose_transition(OUT, IN, out_deck=1, in_deck=2, intent="smooth", top=200)["proposals"]
    }
    assert {"crossfade", "wave", "melt", "rise"} <= styles


@pytest.mark.parametrize("name", sorted(n for n, s in STYLES.items() if s.available and s.hit == "gated"))
def test_a_live_outgoing_fader_left_down_is_never_raised(name: str) -> None:
    """AGENT-35: with the DJ's outgoing fader at 0.4, no bar of any gated blend raises it (replayed every 1/16
    bar), and compile caps every outgoing fader target at 0.4. Control: from a full fader the plan does lower it."""
    from apps.autodj.plan import state_at

    bars = STYLES[name].clamp_bars(STYLES[name].default_bars)
    plan = parse_plan(_plan(name, bars=bars, out_start=OUT.bars - bars) | {"events": style(name).build(bars, 1, 2)})
    start = (ChannelStart(fader=0.4), ChannelStart())
    for step in range(-16, bars * 16 + 1):
        assert state_at(plan, step / 16, bar=math.floor(step / 16), channels=start).out_fader <= 0.4 + 1e-9, (
            name,
            step,
        )
    live = _live_with_faders(0.4, 1.0)
    order = compile_plan(plan, IN, plan_id="p", live=live)["plan"]
    targets = [
        (e.get("command") or e["ramp"]["command"])["value"]
        for e in order["events"]
        if (e.get("command") or e.get("ramp", {}).get("command", {})).get("type") == "fader"
        and (e.get("command") or e["ramp"]["command"]).get("deck") == 1
    ]
    assert targets and max(targets) <= 0.4 and order["notes"] == []
    unknown = compile_plan(plan, IN, plan_id="p")["plan"]
    assert unknown["notes"] and "uncapped" in unknown["notes"][0]


@pytest.mark.parametrize(
    "fader",
    [float("nan"), float("inf"), True, 1.5, -0.1, "0.4", None],
)
def test_a_malformed_live_fader_is_an_error_not_uncapped(fader: object) -> None:
    """AGENT-35 (Sol P1, the class): a live fader that is present but not a 0..1 number raises in compile AND in
    validation's channel read; only an absent fader (or mixer, or channel) is "uncapped" with the note."""
    plan = parse_plan(_plan())
    live = _live_with_faders(0.4, 1.0)
    live["mixer"]["channels"]["1"]["fader"] = fader
    with pytest.raises(ValueError, match="not a level between 0 and 1"):
        compile_plan(plan, IN, plan_id="p", live=live)
    with pytest.raises(ValueError, match="not a level between 0 and 1"):
        validate_plan(plan, OUT, IN, live=live)


def test_an_absent_live_fader_is_uncapped_with_a_note_and_a_malformed_mixer_raises() -> None:
    plan = parse_plan(_plan())
    for live in (_live(), {**_live(), "mixer": {"channels": {}}}, {**_live(), "mixer": {"channels": {"1": {}}}}):
        assert "uncapped" in compile_plan(plan, IN, plan_id="p", live=live)["plan"]["notes"][0]
    with pytest.raises(ValueError, match="not an object"):
        compile_plan(plan, IN, plan_id="p", live={**_live(), "mixer": ["not", "an", "object"]})


#: Every object level of the live mirror the planner reads, and a malformed value at that level.
_MIRROR_LEVELS = {
    "mixer": lambda live: live.__setitem__("mixer", ["not", "an", "object"]),
    "channels": lambda live: live.__setitem__("mixer", {"channels": "x"}),
    "channel": lambda live: live.__setitem__("mixer", {"channels": {"1": 3, "2": {}}}),
    "decks": lambda live: live.__setitem__("decks", ["x"]),
    "deck": lambda live: live["decks"].__setitem__("2", "x"),
    # Sol P1 #7595: an explicit null is malformed, not missing.
    "mixer null": lambda live: live.__setitem__("mixer", None),
    "channels null": lambda live: live.__setitem__("mixer", {"channels": None}),
    "channel null": lambda live: live.__setitem__("mixer", {"channels": {"1": None, "2": {}}}),
    "decks null": lambda live: live.__setitem__("decks", None),
    "deck null": lambda live: live["decks"].__setitem__("2", None),
}


@pytest.mark.parametrize("level", sorted(_MIRROR_LEVELS))
def test_every_live_mirror_level_is_read_strictly_in_validation_and_compile(level: str) -> None:
    """AGENT-35 (Sol P1, the class): one reader (live_mapping) for every object level of the live mirror. A level
    present but not an object raises in validation AND compile; a missing level reads as absent (control)."""
    plan = parse_plan(_plan())
    live = _live_with_faders(1.0, 1.0)
    _MIRROR_LEVELS[level](live)
    with pytest.raises(ValueError, match="not an object"):
        validate_plan(plan, OUT, IN, live=live)
    with pytest.raises(ValueError, match="not an object"):
        compile_plan(plan, IN, plan_id="p", live=live)


def test_missing_mirror_levels_read_as_absent() -> None:
    plan = parse_plan(_plan())
    bare = {
        "master_deck": 1,
        "decks": {"1": {"stable_id": "trk-out", "playing": True, "position": {"ms": OUT.time_of_bar(100) * 1000}}},
    }
    assert compile_plan(plan, IN, plan_id="p", live=bare)["plan"]["notes"]  # no mixer: uncapped, said
    assert validate_plan(plan, OUT, IN, live=bare) is not None


def test_an_explicit_null_position_is_malformed_not_missing() -> None:
    """Sol P1 #7595: ``position: null`` raises; a deck with no position key reads as absent (control)."""
    plan = parse_plan(_plan())
    live = _live_with_faders(1.0, 1.0)
    live["decks"]["1"]["position"] = None
    with pytest.raises(ValueError, match="null, not an object"):
        validate_plan(plan, OUT, IN, live=live)
    del live["decks"]["1"]["position"]
    assert validate_plan(plan, OUT, IN, live=live) is not None


def test_the_trim_matches_the_outgoing_deck_as_it_plays_including_its_trim() -> None:
    """AGENT-31 (Codex P2/BLOCKING #7594): A at -9 gave B at -3 a -6 dB trim; from B (still trimmed -6 dB) to C at -3,
    the match is -6 dB again, not 0 dB (C would be 6 dB louder than B). Control: an untrimmed outgoing deck."""
    from apps.autodj.profile import match_trim_db, trim_knob

    b, c = _loud("trk-out", -3.0), _loud("trk-in", -3.0)
    assert match_trim_db(b, c)[0] == 0.0
    assert match_trim_db(b, c, -6.0)[0] == pytest.approx(-6.0)
    live = _live()
    live["mixer"] = {"channels": {"1": {"trim": trim_knob(-6.0)}}}
    rows = propose_transition(b, c, out_deck=1, in_deck=2, intent="smooth", live=live)["proposals"]
    assert rows and all(row["plan"]["in"]["trim_db"] == pytest.approx(-6.0, abs=0.01) for row in rows)
    live["mixer"]["channels"]["1"]["trim"] = "loud"
    with pytest.raises(ValueError, match="not a knob value"):
        propose_transition(b, c, out_deck=1, in_deck=2, intent="smooth", live=live)


def test_match_trim_includes_live_auto_gain_offsets() -> None:
    """AGENT-31 (#7654): Codex pair with auto gain returns 0 dB; without auto gain still +6 dB."""
    from apps.autodj.profile import match_trim_db

    out_p, in_p = _loud("trk-out", -6.0), _loud("trk-in", -12.0)
    assert match_trim_db(out_p, in_p, 0.0, out_auto_gain_db=-6.0, in_auto_gain_db=0.0) == (
        0.0,
        "match -12.0 to -6.0 LUFS with the outgoing trim +0.0 dB and auto gain -6.0/+0.0 dB (-12.0 LUFS): +0.0 dB",
    )
    assert match_trim_db(out_p, in_p)[0] == pytest.approx(6.0)


def test_both_decks_auto_gained_to_the_same_target_need_no_trim() -> None:
    from apps.autodj.profile import match_trim_db

    out_p, in_p = _loud("trk-out", -6.0), _loud("trk-in", -6.0)
    assert match_trim_db(out_p, in_p, 0.0, out_auto_gain_db=-6.0, in_auto_gain_db=-6.0)[0] == 0.0


def _mirror_with_auto_gain() -> dict[str, Any]:
    live = _live()
    live["mixer"] = {"channels": {"1": {"trim": 0.5}, "2": {"trim": 0.5}}}
    live["auto_gain"] = {
        "1": {"state": "ready", "value": {"stable_id": "trk-out", "effective_offset_db": -6.0}},
        "2": {"state": "ready", "value": {"stable_id": "trk-in", "effective_offset_db": 0.0}},
    }
    return live


def test_live_propose_matches_loudness_with_auto_gain_on_the_mirror() -> None:
    out_p, in_p = _loud("trk-out", -6.0), _loud("trk-in", -12.0)
    rows = propose_transition(out_p, in_p, out_deck=1, in_deck=2, intent="smooth", live=_mirror_with_auto_gain())[
        "proposals"
    ]
    assert rows and all(row["plan"]["in"]["trim_db"] == pytest.approx(0.0) for row in rows)
    live_no_ag = _live()
    live_no_ag["mixer"] = {"channels": {"1": {"trim": 0.5}, "2": {"trim": 0.5}}}
    without = propose_transition(out_p, in_p, out_deck=1, in_deck=2, intent="smooth", live=live_no_ag)["proposals"]
    assert without and all(row["plan"]["in"]["trim_db"] == pytest.approx(6.0) for row in without)
    assert "the live mirror carries no auto_gain" in without[0]["plan"]["in"]["trim_reason"]


def test_stale_auto_gain_ready_is_treated_as_zero_db() -> None:
    from apps.autodj.propose import _deck_auto_gain_db

    live = _mirror_with_auto_gain()
    live["auto_gain"]["1"]["value"]["stable_id"] = "other"
    offset, note = _deck_auto_gain_db(live, 1, "trk-out")
    assert offset == 0.0 and note is not None and "other" in note and "trk-out" in note
    live = _mirror_with_auto_gain()
    live["decks"]["1"]["stable_id"] = "other"
    offset, note = _deck_auto_gain_db(live, 1, "trk-out")
    assert offset == 0.0 and note is not None


def test_auto_gain_error_and_empty_play_at_zero_db() -> None:
    from apps.autodj.propose import _deck_auto_gain_db

    live = _mirror_with_auto_gain()
    live["auto_gain"]["1"] = {"state": "error", "stable_id": "trk-out", "message": "boom"}
    assert _deck_auto_gain_db(live, 1, "trk-out") == (0.0, "deck 1 auto gain error, so the match assumes 0 dB")
    live["auto_gain"]["1"] = {"state": "empty"}
    assert _deck_auto_gain_db(live, 1, "trk-out") == (0.0, None)


@pytest.mark.parametrize(
    "auto_gain",
    [
        "not-an-object",
        {"2": {"state": "ready", "value": {"stable_id": "trk-in", "effective_offset_db": 0.0}}},
        {"1": {"state": "weird"}},
        {"1": {"state": "ready"}},
        {"1": {"state": "ready", "value": {"stable_id": "trk-out", "effective_offset_db": True}}},
        {"1": {"state": "ready", "value": {"stable_id": "trk-out", "effective_offset_db": "x"}}},
        {"1": {"state": "ready", "value": {"stable_id": "trk-out", "effective_offset_db": float("nan")}}},
        {"1": {"state": "ready", "value": {"stable_id": "trk-out", "effective_offset_db": 13.0}}},
    ],
)
def test_malformed_present_auto_gain_raises(auto_gain: Any) -> None:
    from apps.autodj.propose import _deck_auto_gain_db

    live = _mirror_with_auto_gain()
    live["auto_gain"] = auto_gain
    with pytest.raises(
        ValueError,
        match=r"not an object|a partial mirror is refused|not a finite offset|unknown",
    ):
        _deck_auto_gain_db(live, 1, "trk-out")


def test_offline_and_missing_auto_gain_key_assume_zero_db() -> None:
    from apps.autodj.propose import _deck_auto_gain_db

    assert _deck_auto_gain_db(None, 1, "trk-out") == (
        0.0,
        "offline proposal: no live auto gain, so the match assumes 0 dB",
    )
    assert _deck_auto_gain_db({"decks": {}}, 1, "trk-out") == (
        0.0,
        "the live mirror carries no auto_gain, so the match assumes 0 dB",
    )
    offset, note = _deck_auto_gain_db(_mirror_with_auto_gain(), 1, "trk-out")
    assert offset == pytest.approx(-6.0) and note is None


def test_missing_auto_gain_note_is_deduped_for_both_decks() -> None:
    from apps.autodj.propose import match_live_trim_db

    live = _live()
    live["mixer"] = {"channels": {"1": {"trim": 0.5}, "2": {"trim": 0.5}}}
    out_p, in_p = _loud("trk-out", -6.0), _loud("trk-in", -12.0)
    _, reason = match_live_trim_db(out_p, in_p, live, 1, 2)
    assert reason.count("the live mirror carries no auto_gain") == 1


def test_a_glide_after_the_handover_stays_on_the_incoming_deck() -> None:
    """AGENT-32 (Sol P1 #7594): from the terminal master event on, the incoming deck is the leader, so a tempo move
    there stays on it; one before moves to the outgoing leader (control); a ramp straddling the handover is refused."""
    live = _live_glide(128.0, 128.0 / 125.0)
    document = _plan()
    handover = max(e["at_bar"] for e in document["events"] if e.get("do") == "master")
    document["events"] += [
        {"at_bar": 3, "deck": 2, "set": {"tempo": 1.02}},
        {"at_bar": handover, "deck": 2, "set": {"tempo": 1.01}},
    ]
    events = compile_plan(parse_plan(document), IN, plan_id="p", live=live)["plan"]["events"]
    tempos = [(e["at_bar"], e["command"]["deck"]) for e in events if e.get("command", {}).get("type") == "tempo"]
    assert (3, 1) in tempos and (handover, 2) in tempos
    straddle = _plan()
    straddle["events"].append({"at_bar": handover - 1, "deck": 2, "ramp": {"tempo": 1.02}, "over_bars": 1})
    straddle["events"][-1]["over_bars"] = 1  # ends exactly at the handover: allowed
    compile_plan(parse_plan(straddle), IN, plan_id="p", live=live)
    straddle["events"][-1]["at_bar"] = handover - 0.5
    with pytest.raises(ValueError, match="runs past the handover"):
        compile_plan(parse_plan(straddle), IN, plan_id="p", live=live)


@pytest.mark.parametrize("deck", ["1", "2"])
@pytest.mark.parametrize("key", ["fader", "trim", "eq_low"])
@pytest.mark.parametrize("bad", [float("nan"), True, 1.5, "x"])
def test_compile_refuses_any_malformed_live_level_on_either_deck(deck: str, key: str, bad: object) -> None:
    """AGENT-35 (Sol P1 #7595, the class): compile reads both plan channels' fader, trim and low EQ through the same
    strict reader as validation, so any malformed present value refuses the order; a well-formed mirror compiles."""
    plan = parse_plan(_plan())
    live = _live_with_faders(1.0, 1.0)
    compile_plan(plan, IN, plan_id="p", live=live)  # control
    live["mixer"]["channels"][deck][key] = bad
    with pytest.raises(ValueError, match="not a level between 0 and 1"):
        compile_plan(plan, IN, plan_id="p", live=live)


@pytest.mark.requirement("AGENT-32")
def test_a_synced_glide_is_validated_on_the_leader_led_grid() -> None:
    """[if] a synced plan glides the incoming tempo before the handover [then] an exact-fit plan is not refused as running out of grid, [else stop].

    Codex #7594: validation counts the incoming grid at the follower's fixed ratio to the clock (the leader moves, as
    compile emits); the same glide unsynced still needs the extra bars (control)."""
    document = _plan(bars=15)
    document["in"]["window"]["start_bar"] = IN.bars - 14
    document["events"].append({"at_bar": 0, "deck": 2, "set": {"tempo": 1.08}})
    errors = validate_plan(parse_plan(document), OUT, IN).errors
    assert not any("runs out of grid" in e for e in errors), errors
    document["tempo"] = {"mode": "none"}
    unsynced = validate_plan(parse_plan(document), OUT, IN).errors
    assert any("runs out of grid" in e for e in unsynced), unsynced


@pytest.mark.requirement("AGENT-32")
def test_a_tempo_set_at_the_handover_bar_but_before_the_master_event_moves_onto_the_leader() -> None:
    """[if] an incoming tempo set sits at the handover's bar but before its master event [then] it goes on the leader, [else stop].

    Codex #7594: it runs while the incoming deck still follows; authored after the master event it stays (control)."""
    document = _plan()
    master = next(i for i, e in enumerate(document["events"]) if e.get("do") == "master" and e.get("deck") == 2)
    bar = document["events"][master]["at_bar"]
    live = _live_glide(128.0, 128.0 / 125.0)

    def tempo_decks(events: list[dict[str, Any]]) -> list[int]:
        order = compile_plan(parse_plan({**document, "events": events}), IN, plan_id="plan-test", live=live)["plan"]
        rows = [e.get("command") or (e.get("ramp") or {}).get("command") or {} for e in order["events"]]
        return [c["deck"] for c in rows if c.get("type") == "tempo"]

    before = [
        *document["events"][:master],
        {"at_bar": bar, "deck": 2, "set": {"tempo": 1.02}},
        *document["events"][master:],
    ]
    assert tempo_decks(before) == [1]
    after = [
        *document["events"][: master + 1],
        {"at_bar": bar, "deck": 2, "set": {"tempo": 1.02}},
        *document["events"][master + 1 :],
    ]
    assert tempo_decks(after) == [2]


from apps.autodj.propose import INTENTS as INTENTS_ALL  # noqa: E402


@pytest.mark.parametrize("intent", INTENTS_ALL)
def test_every_intent_offers_all_four_parameter_styles_after_its_older_styles(intent: str) -> None:
    """Sol P1 #7595 (AGENT-34): [if] an intent is asked for [then] crossfade, rise, wave and melt are all candidates,
    each after every older style, so the older styles' preference bonus does not move."""
    from apps.autodj.propose import _PREFERENCES, PARAM_STYLES

    names = [name for name, _bars in _PREFERENCES[intent]]
    assert set(PARAM_STYLES) <= set(names), names
    first_param = min(names.index(n) for n in PARAM_STYLES)
    assert all(n in PARAM_STYLES for n in names[first_param:]), names


def test_validation_caps_the_outgoing_fader_once_per_replay_not_per_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex #7595 (perf): [if] the outgoing fader starts below 1 [then] a 64-bar validation caps the plan a handful of
    times (once per replay), never once per silence sample, which cost seconds and could let the start pass."""
    import apps.autodj.plan as plan_module

    calls = 0
    real = plan_module.cap_outgoing_fader

    def counting(plan: Any, ceiling: float) -> Any:
        nonlocal calls
        calls += 1
        return real(plan, ceiling)

    monkeypatch.setattr(plan_module, "cap_outgoing_fader", counting)
    document = _plan("long_blend", bars=64, out_start=50)
    validate_plan(parse_plan(document), OUT, IN, live=_live_with_faders(0.4, 1.0))
    assert 0 < calls <= 10, calls


def test_a_missing_or_malformed_fader_value_is_refused_not_read_as_a_kill() -> None:
    """[if] an outgoing fader event has no numeric value [then] capping refuses it rather than killing the deck, [else stop].

    Sol P1 #7595 (the class of ``value or 0.0``): every set and ramp value is read through PlanEvent.target."""
    from apps.autodj.plan import PlanEvent, cap_outgoing_fader

    plan = parse_plan(_plan())
    for bad in (None, float("nan"), True):
        broken = replace(
            plan, events=(*plan.events, PlanEvent(at_bar=1.0, deck=1, kind="set", name="fader", value=bad))
        )
        with pytest.raises(ValueError, match="no numeric value"):
            cap_outgoing_fader(broken, 0.5)
    capped = cap_outgoing_fader(
        replace(plan, events=(PlanEvent(at_bar=1.0, deck=1, kind="set", name="fader", value=0.9),)), 0.5
    )
    assert capped.events[0].value == 0.5  # control: a valid level is capped as before


@pytest.mark.requirement("AGENT-32")
def test_a_synced_tempo_move_before_the_incoming_launch_is_refused() -> None:
    """[if] a synced plan moves the incoming tempo before the incoming launch [then] validation and compile refuse it, [else stop].

    Codex #7594: the move would go to the leader while the incoming deck is paused, and a quantized launch does not
    retime the follower, so it would start at the old tempo and drift. Controls: the same move after the launch, and
    the same plan unsynced, are accepted."""
    document = _plan()
    document["events"] = [
        {**e, "at_bar": 2} if e.get("do") == "play" and e.get("deck") == 2 else e for e in document["events"]
    ]
    live = _live_glide(128.0, 128.0 / 125.0)
    early = {**document, "events": [{"at_bar": 0, "deck": 2, "set": {"tempo": 1.02}}, *document["events"]]}
    assert any("before the incoming launch" in e for e in validate_plan(parse_plan(early), OUT, IN).errors)
    with pytest.raises(ValueError, match="before the incoming launch"):
        compile_plan(parse_plan(early), IN, plan_id="p", live=live)
    late = {**document, "events": [*document["events"], {"at_bar": 3, "deck": 2, "set": {"tempo": 1.02}}]}
    assert not any("before the incoming launch" in e for e in validate_plan(parse_plan(late), OUT, IN).errors)
    compile_plan(parse_plan(late), IN, plan_id="p", live=live)
    unsynced = {**early, "tempo": {"mode": "none"}}
    assert not any("before the incoming launch" in e for e in validate_plan(parse_plan(unsynced), OUT, IN).errors)


@pytest.mark.requirement("AGENT-32")
def test_a_synced_tempo_move_after_the_play_command_but_before_its_downbeat_is_refused() -> None:
    """[if] a synced tempo move sits after the authored play but before the launch downbeat [then] it is refused, [else stop].

    Sol P1 #7594: the play is dispatched early and the deck starts on the next downbeat (play_start_bar), so a move
    between the two still runs while the follower is paused. Control: the same move on the launch downbeat is accepted."""
    document = _plan()
    document["events"] = [
        {**e, "at_bar": 2.5} if e.get("do") == "play" and e.get("deck") == 2 else e for e in document["events"]
    ]
    between = {**document, "events": [*document["events"], {"at_bar": 2.6, "deck": 2, "set": {"tempo": 1.02}}]}
    assert any("before the incoming launch" in e for e in validate_plan(parse_plan(between), OUT, IN).errors)
    on_start = {**document, "events": [*document["events"], {"at_bar": 3, "deck": 2, "set": {"tempo": 1.02}}]}
    assert not any("before the incoming launch" in e for e in validate_plan(parse_plan(on_start), OUT, IN).errors)


@pytest.mark.parametrize(
    "live",
    [
        {"mixer": None},
        {"mixer": {}},
        {"mixer": {"channels": None}},
        {"mixer": {"channels": {}}},
        {"mixer": {"channels": {"1": None}}},
        {"mixer": {"channels": {"1": {}}}},
        {"mixer": {"channels": {"1": {"trim": None}}}},
        {"mixer": {"channels": {"1": {"trim": True}}}},
        {"mixer": {"channels": {"1": {"trim": 1.5}}}},
        {"mixer": {"channels": {"1": {"trim": float("nan")}}}},
    ],
)
def test_a_partial_or_malformed_live_mixer_is_refused_never_read_as_unity_trim(live: dict[str, Any]) -> None:
    """[if] the live mirror has a mixer but its outgoing trim is missing or malformed [then] propose refuses it, [else stop].

    Sol P1 #7594: chained ``or {}`` fallbacks read a partial mirror as a 0 dB outgoing trim."""
    from apps.autodj.propose import _outgoing_trim_db

    with pytest.raises(
        ValueError,
        match=r"not an object|a partial mirror is refused|not a knob value",
    ):
        _outgoing_trim_db(live, 1)


def test_default_outgoing_trim_is_used_offline_or_without_a_mixer() -> None:
    """[if] there is no live mirror, or it carries no mixer [then] the outgoing trim is assumed, with its reason, [else stop].

    Offline and incomplete live mirrors use the shared deck-load trim."""
    from apps.autodj.profile import trim_knob
    from apps.autodj.propose import _outgoing_trim_db

    db, reason = _outgoing_trim_db(None, 1)
    assert db == -6.0 and "offline" in (reason or "")
    assert "no mixer" in (_outgoing_trim_db({"decks": {}}, 1)[1] or "")
    assert _outgoing_trim_db({"decks": {}}, 1)[0] == -6.0
    db, reason = _outgoing_trim_db({"mixer": {"channels": {"1": {"trim": trim_knob(-6.0)}}}}, 1)
    assert db == pytest.approx(-6.0, abs=0.01) and reason is None  # control: a real trim is read as is


@pytest.mark.parametrize("ceiling", [1.0, 0.5])
def test_an_out_of_range_outgoing_fader_target_is_refused_whatever_the_live_ceiling(ceiling: float) -> None:
    """[if] an outgoing fader target is outside 0..1 [then] capping refuses it at a unity ceiling too, [else stop].

    Sol P1 #7595: the unity fast path returned before the level check."""
    from apps.autodj.plan import PlanEvent, cap_outgoing_fader

    plan = parse_plan(_plan())
    for bad in (1.5, -0.1):
        broken = replace(plan, events=(PlanEvent(at_bar=1.0, deck=1, kind="set", name="fader", value=bad),))
        with pytest.raises(ValueError, match="not a level between 0 and 1"):
            cap_outgoing_fader(broken, ceiling)
    ok = replace(plan, events=(PlanEvent(at_bar=1.0, deck=1, kind="set", name="fader", value=0.9),))
    assert cap_outgoing_fader(ok, ceiling).events[0].value == min(0.9, ceiling)  # control


@pytest.mark.parametrize("live_fader", [None, 0.5])
def test_compile_refuses_an_out_of_range_outgoing_fader_with_or_without_a_live_ceiling(
    live_fader: float | None,
) -> None:
    """[if] a planned outgoing fader is outside 0..1 [then] compile refuses it, also when the mirror has no live fader.

    Sol P1 #7595: the no-ceiling branch skipped the check. Control: with a live ceiling, a valid target is still capped."""
    from apps.autodj.plan import PlanEvent

    live = _live()
    if live_fader is not None:
        live["mixer"] = {"channels": {"1": {"fader": live_fader}}}
    plan = parse_plan(_plan())
    broken = replace(plan, events=(*plan.events, PlanEvent(at_bar=1.0, deck=1, kind="set", name="fader", value=1.5)))
    with pytest.raises(ValueError, match="not a level between 0 and 1"):
        compile_plan(broken, IN, plan_id="p", live=live)
    if live_fader is not None:
        ok = replace(plan, events=(*plan.events, PlanEvent(at_bar=1.0, deck=1, kind="set", name="fader", value=0.9)))
        rows = compile_plan(ok, IN, plan_id="p", live=live)["plan"]["events"]
        commands = [r.get("command") or r.get("ramp", {}).get("command") for r in rows]
        faders = [c["value"] for c in commands if c and c.get("type") == "fader" and c.get("deck") == 1]
        assert faders and max(faders) <= live_fader  # the live ceiling still caps


#: The older styles' preference bonuses as af--autodj-defects computes them (0.06 x (list length - rank)), per intent.
_OLDER_BONUSES = {
    "smooth": [0.24, 0.18, 0.12, 0.06],
    "energy_up": [0.24, 0.18, 0.12, 0.06],
    "wind_down": [0.18, 0.12, 0.06],
    "showy": [0.24, 0.18, 0.12, 0.06],
    "vocals_clean": [0.24, 0.18, 0.12, 0.06],
}


@pytest.mark.parametrize("intent", sorted(_OLDER_BONUSES))
def test_older_style_preference_bonuses_are_unchanged_by_the_parameter_styles(intent: str) -> None:
    """[if] the parameter styles are appended to an intent [then] every older style keeps its bonus, and the parameter
    styles score none, [else stop]. Sol P1 #7595: the bonus counted the whole list, so the older ones moved up 0.24."""
    from apps.autodj.propose import _PREFERENCES, PARAM_STYLES, _preference_bonus

    older = _OLDER_BONUSES[intent]
    bonuses = [round(_preference_bonus(intent, rank), 6) for rank in range(len(_PREFERENCES[intent]))]
    assert bonuses[: len(older)] == older
    assert all(b == 0.0 for b in bonuses[len(older) :])  # control: the parameter styles add no bonus
    assert {name for name, _bars in _PREFERENCES[intent][len(older) :]} <= set(PARAM_STYLES)


def test_propose_scores_every_candidate_with_the_pinned_preference_bonus(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] propose scores a candidate [then] its preference bonus comes from ``_preference_bonus``, the pinned values
    above, [else stop]: a formula inlined back into the loop would bypass the pin (Sol P1 #7595)."""
    from apps.autodj import propose

    seen: list[tuple[str, int]] = []
    real = propose._preference_bonus

    def spy(intent: str, rank: int) -> float:
        seen.append((intent, rank))
        return real(intent, rank)

    monkeypatch.setattr(propose, "_preference_bonus", spy)
    result = propose.propose_transition(OUT, IN, out_deck=1, in_deck=2, intent="smooth")
    assert result["considered"] > 0 and len(seen) == result["considered"]
    assert {intent for intent, _rank in seen} == {"smooth"}


def test_an_incoming_trim_before_the_quantized_launch_downbeat_is_not_heard() -> None:
    """[if] a trim lands after the incoming play is authored but before its quantized downbeat [then] the deck is not
    playing yet and the trim passes; one after the downbeat is refused (control), [else stop]. Sol P1 #7594: the replay
    started the deck at the authored play bar, not at ``play_start_bar``, the launch compile dispatches."""
    from apps.autodj.plan import play_start_bar

    start = play_start_bar(0.5, None)
    assert start > 0.75, start  # the fixture's premise: bar 0.75 is between the authored play and its downbeat

    def trim_errors(trim_bar: float) -> list[str]:
        document = _plan()
        document["events"] = [
            {"at_bar": 0.5, "deck": 2, "do": "play"},
            {"at_bar": trim_bar, "deck": 2, "set": {"trim": 0.4}},
            *[e for e in document["events"] if e.get("deck") != 2 or e.get("do") == "master"],
        ]
        return [e for e in validate_plan(parse_plan(document), OUT, IN).errors if "trim" in e]

    assert trim_errors(0.75) == []  # valid only once the launch is quantized
    assert trim_errors(start + 0.5)  # control: after the downbeat the deck plays, so the trim is heard either way


def test_the_incoming_deck_leads_only_from_the_terminal_master_handover() -> None:
    """[if] a plan hands master to the incoming deck twice [then] tempo moves between the two still run on the
    leader, and only those after the terminal handover stay on the incoming deck, [else stop]. Sol P1 #7594: the first
    handover was taken. Control: with one handover, a move after it stays on the incoming deck."""
    from apps.autodj.plan import handover_bar, incoming_leads

    def plan_with(*masters: float) -> Any:
        document = _plan()
        document["events"] = [e for e in document["events"] if e.get("do") != "master"] + [
            *({"at_bar": bar, "deck": 2, "do": "master"} for bar in masters),
            {"at_bar": 6, "deck": 2, "set": {"tempo": 1.01}},
        ]
        return parse_plan(document)

    twice = plan_with(4, 8)
    tempo = next(i for i, e in enumerate(twice.events) if e.name == "tempo")
    assert handover_bar(twice) == 8 and incoming_leads(twice, tempo) is False
    once = plan_with(4)
    tempo = next(i for i, e in enumerate(once.events) if e.name == "tempo")
    assert handover_bar(once) == 4 and incoming_leads(once, tempo) is True


@pytest.mark.parametrize(
    ("authored", "previous", "start"),
    [
        (0.62, None, 1.0),  # dispatched at 0.37, starts on the next downbeat
        (1.0, None, 1.0),  # dispatched at 0.75
        (0.25, None, 1.0),  # dispatched at bar 0: that downbeat has passed, so the next one
        (-1.0, None, 0.0),  # clamped to the earliest bar
        (2.1, 2.0, 3.0),  # never dispatched before the deck's previous transport event at bar 2
    ],
)
def test_play_start_bar_is_the_one_quantized_launch_time(authored: float, previous: float | None, start: float) -> None:
    """[if] a quantized play is authored at a bar [then] ``play_start_bar`` gives the downbeat the deck starts on, the
    one helper validation, compile and the renderer share, [else stop]."""
    from apps.autodj.plan import launch_bar, play_start_bar

    assert play_start_bar(authored, previous) == start
    assert play_start_bar(authored, previous) == int(launch_bar(authored, previous) // 1) + 1


def test_an_incoming_trim_ramp_running_past_the_launch_downbeat_is_heard() -> None:
    """[if] an incoming trim ramp starts while the deck is cued but runs past its quantized downbeat [then] it is
    refused, [else stop] (blind review on #7594). Control: a ramp that ends before the downbeat passes."""
    from apps.autodj.plan import play_start_bar

    start = play_start_bar(4.0, None)

    def trim_errors(ramp_bar: float, over: float) -> list[str]:
        document = _plan()
        document["events"] = [
            {"at_bar": 4.0, "deck": 2, "do": "play"},
            {"at_bar": ramp_bar, "deck": 2, "ramp": {"trim": 0.8}, "over_bars": over},
            *[e for e in document["events"] if e.get("deck") != 2 or e.get("do") == "master"],
        ]
        return [e for e in validate_plan(parse_plan(document), OUT, IN).errors if "trim" in e]

    assert trim_errors(start - 1.0, 2.0)  # runs from cued into playing: the room hears the swell
    assert trim_errors(start - 1.0, 0.5) == []  # control: done before the downbeat


def test_trim_validation_replays_same_bar_transport_in_authored_order() -> None:
    """[if] a trim and a stop share a bar [then] authored order decides whether the deck is audible when the trim
    lands, and a play counts from where the page dispatches it, [else stop]. Sol P1 #7594: the replay asked the whole
    event list about that bar, so a later stop made an earlier audible trim look safe."""
    from apps.autodj.plan import play_start_bar

    def trim_errors(*events: dict[str, Any]) -> list[str]:
        document = _plan()
        document["events"] = [
            *events,
            *[e for e in document["events"] if e.get("deck") != 2 or e.get("do") == "master"],
        ]
        return [e for e in validate_plan(parse_plan(document), OUT, IN).errors if "trim" in e]

    play = {"at_bar": 0.5, "deck": 2, "do": "play"}  # starts on bar 1
    trim2 = {"at_bar": 2, "deck": 2, "set": {"trim": 0.4}}
    stop2 = {"at_bar": 2, "deck": 2, "do": "stop"}
    assert trim_errors(play, trim2, stop2)  # authored before the stop: the deck is still audible
    assert trim_errors(play, stop2, trim2) == []  # control: after the stop it is quiet
    # A play runs at its dispatch bar (launch_bar, ahead of its authored bar), not where it is written: authored at
    # 16.1 it starts on bar 16, so a trim written at 16.05 lands on an audible deck in either authored order.
    late_play = {"at_bar": 16.1, "deck": 2, "do": "play"}
    assert play_start_bar(16.1, None) == 16.0
    assert trim_errors({"at_bar": 16.05, "deck": 2, "set": {"trim": 0.4}}, late_play)
    assert (
        trim_errors({"at_bar": 15.9, "deck": 2, "set": {"trim": 0.4}}, late_play) == []
    )  # control: before the downbeat


def test_a_trim_ramp_is_judged_across_its_span_as_the_fader_moves() -> None:
    """[if] the incoming fader opens during a trim ramp while the deck plays, or before the deck starts inside it
    [then] the ramp is heard; if the fader closes before the deck starts inside it, it is not, [else stop]. Blind
    review on #7594: the ramp was judged only at its first bar."""

    def trim_errors(*events: dict[str, Any], fader: float) -> list[str]:
        document = _plan()
        document["events"] = [
            {"at_bar": 0, "deck": 2, "set": {"fader": fader}},
            *events,
            *[e for e in document["events"] if e.get("deck") != 2 or e.get("do") == "master"],
        ]
        return [e for e in validate_plan(parse_plan(document), OUT, IN).errors if "trim" in e]

    ramp = {"at_bar": 0.5, "deck": 2, "ramp": {"trim": 0.3}, "over_bars": 6}
    play4 = {"at_bar": 4, "deck": 2, "do": "play"}  # starts on bar 4, inside the ramp
    assert trim_errors(
        ramp, {"at_bar": 2, "deck": 2, "set": {"fader": 1.0}}, play4, fader=0.0
    )  # opened before the start
    assert trim_errors(ramp, {"at_bar": 2, "deck": 2, "set": {"fader": 0.0}}, play4, fader=1.0) == []  # closed first
    playing = {"at_bar": 0.25, "deck": 2, "do": "play"}  # playing from bar 1
    late_ramp = {"at_bar": 2, "deck": 2, "ramp": {"trim": 0.3}, "over_bars": 4}
    assert trim_errors(playing, late_ramp, {"at_bar": 3, "deck": 2, "set": {"fader": 1.0}}, fader=0.0)  # opens mid-ramp
    assert trim_errors(playing, late_ramp, {"at_bar": 7, "deck": 2, "set": {"fader": 1.0}}, fader=0.0) == []  # after it


def test_a_fader_closed_before_the_downbeat_is_closed_when_the_launch_is_judged() -> None:
    """[if] a closing fader ramp ends between a play's dispatch and its downbeat [then] the trim ramp the downbeat
    falls inside is not heard; with the fader left open it is (control), [else stop]. Blind review on #7594."""

    def trim_errors(close_fader: bool) -> list[str]:
        document = _plan()
        document["events"] = [
            {"at_bar": 1, "deck": 2, "do": "play"},
            *([{"at_bar": 9.75, "deck": 2, "ramp": {"fader": 0.0}, "over_bars": 1}] if close_fader else []),
            {"at_bar": 10, "deck": 2, "do": "stop"},
            {"at_bar": 10.5, "deck": 2, "do": "play"},  # dispatched at 10 (after the stop), starts on bar 11
            {"at_bar": 10.6, "deck": 2, "ramp": {"trim": 0.8}, "over_bars": 2},
            {"at_bar": 12, "deck": 2, "set": {"fader": 0.0}},
            *[e for e in document["events"] if e.get("deck") != 2 or e.get("do") == "master"],
        ]
        return [e for e in validate_plan(parse_plan(document), OUT, IN).errors if "trim" in e]

    assert trim_errors(close_fader=True) == []
    assert trim_errors(close_fader=False)  # control: the fader is open at the downbeat


def test_a_trim_set_judged_safe_is_not_judged_again_when_the_fader_opens_on_its_bar() -> None:
    """[if] a trim set lands while the fader is shut and a later event on the same bar opens it [then] the trim stays
    safe, [else stop] (blind review on #7594: a set lingered in the walk and was judged again). Control: opening the
    fader before the trim on that bar makes it heard."""

    def trim_errors(*events: dict[str, Any]) -> list[str]:
        document = _plan()
        document["events"] = [
            {"at_bar": 0.25, "deck": 2, "do": "play"},
            {"at_bar": 2, "deck": 2, "set": {"fader": 0.0}},
            *events,
            *[e for e in document["events"] if e.get("deck") != 2 or e.get("do") == "master"],
        ]
        return [e for e in validate_plan(parse_plan(document), OUT, IN).errors if "trim" in e]

    trim, open_fader = {"at_bar": 4, "deck": 2, "set": {"trim": 0.5}}, {"at_bar": 4, "deck": 2, "set": {"fader": 1.0}}
    assert trim_errors(trim, open_fader) == []
    assert trim_errors(open_fader, trim)  # control


def test_the_first_command_is_measured_where_the_page_dispatches_it() -> None:
    """[if] a plan's earliest event is a quantized play [then] its first command is measured at ``launch_bar``, ahead
    of its authored bar, so a live position inside that margin is refused, [else stop]. Sol P1 #7595. Control: the same
    position with the play authored as a plain set (no lead) passes the position check."""
    from apps.autodj.plan import first_dispatch_bar, launch_bar

    def plan_with(first: dict[str, Any]) -> Any:
        document = _plan()
        document["events"] = [first, *[e for e in document["events"] if e.get("at_bar", 0) > 0.3]]
        return parse_plan(document)

    played = plan_with({"at_bar": 0.3, "deck": 2, "do": "play"})
    assert first_dispatch_bar(played) == launch_bar(0.3, None) < 0.3
    plain = plan_with({"at_bar": 0.3, "deck": 2, "set": {"trim": 0.5}})
    assert first_dispatch_bar(plain) == 0.3
    start = played.out.start_bar
    bar_now = start + launch_bar(0.3, None) - 0.5 + 0.1  # inside the margin of the dispatch, outside the authored bar's
    live = _live(position={"ms": OUT.time_of_bar(bar_now) * 1000})
    assert any("already at bar" in e for e in validate_plan(played, OUT, IN, live=live).errors)
    assert not any("already at bar" in e for e in validate_plan(plain, OUT, IN, live=live).errors)


@pytest.mark.requirement("AGENT-29")
def test_an_outgoing_tempo_set_before_the_launch_refreshes_the_incoming_tempo() -> None:
    """[if] the outgoing tempo changes before the incoming play [then] the incoming tempo is set again at that bar, [else stop]."""
    live = {
        "master_deck": 1,
        "decks": {
            "1": {"stable_id": "trk-out", "playing": True, "effective_bpm": 120.0, "pitch": 1.0, "pitch_range": 8.0},
            "2": {"stable_id": "trk-in", "playing": False, "pitch_range": 8.0},
        },
    }
    document = _plan()
    document["events"] += [{"at_bar": -0.75, "deck": 1, "set": {"tempo": 1.04}}, {"at_bar": 0, "deck": 2, "do": "play"}]
    events = compile_plan(parse_plan(document), IN, plan_id="plan-test", live=live)["plan"]["events"]
    expected = sync_tempo_ratio({"effective_bpm": 120.0 * 1.04}, IN, 8.0)
    assert expected is not None
    refreshed = [e for e in events if e.get("command") == {"type": "tempo", "deck": 2, "ratio": round(expected, 6)}]
    assert refreshed and refreshed[0]["at_bar"] == -0.75
    # Control: without the outgoing tempo set there is no refresh, only the prepare-time ratio.
    lone = _plan()
    lone["events"].append({"at_bar": 0, "deck": 2, "do": "play"})
    quiet = compile_plan(parse_plan(lone), IN, plan_id="plan-test", live=live)["plan"]["events"]
    assert not [e for e in quiet if e.get("command", {}).get("type") == "tempo"]


@pytest.mark.requirement("AGENT-29")
def test_the_launch_tempo_refresh_keeps_an_authored_incoming_tempo() -> None:
    """[if] the incoming deck has an authored tempo move [then] the outgoing refresh does not overwrite it, [else stop]."""
    live = {
        "master_deck": 1,
        "decks": {
            "1": {"stable_id": "trk-out", "playing": True, "effective_bpm": 120.0, "pitch": 1.0, "pitch_range": 8.0},
            "2": {"stable_id": "trk-in", "playing": False, "pitch_range": 8.0},
        },
    }
    document = _plan()
    document["events"] += [
        {"at_bar": -0.75, "deck": 1, "set": {"tempo": 1.04}},
        {"at_bar": 0, "deck": 2, "set": {"tempo": 1.02}},
        {"at_bar": 0, "deck": 2, "do": "play"},
    ]
    # Merge of #5822 into #7594: AGENT-32 refuses a synced plan that moves the tempo on both decks (the incoming moves
    # go to the leader), so such a plan never reaches the refresh; the authored incoming tempo is never overwritten.
    with pytest.raises(ValueError, match="moves the tempo on one deck only"):
        compile_plan(parse_plan(document), IN, plan_id="plan-test", live=live)


@pytest.mark.requirement("AGENT-29")
def test_the_incoming_prepare_seek_lands_exactly_under_coarse_quantize() -> None:
    """[if] the incoming deck is prepared at its window [then] its seek is exact, so grid quantize cannot re-snap it, [else stop]."""
    prepare = compile_plan(parse_plan(_plan()), IN, plan_id="plan-test")["plan"]["prepare"]
    seeks = [c for c in prepare if c.get("type") == "seek" and c.get("deck") == 2]
    assert seeks and seeks[0].get("exact") is True


@pytest.mark.requirement("AGENT-29")
def test_validation_refuses_an_outgoing_deck_trapped_in_an_engaged_loop() -> None:
    """[if] the outgoing deck has an engaged loop [then] validation refuses the plan, since its position never reaches the window, [else stop]."""
    looped = _live()
    looped["decks"]["1"]["loop_engaged"] = True
    assert any("engaged loop" in error for error in validate_plan(parse_plan(_plan()), OUT, IN, live=looped).errors)
    # Control: the same deck with its loop released validates the window.
    released = _live()
    released["decks"]["1"]["loop_engaged"] = False
    released_errors = validate_plan(parse_plan(_plan()), OUT, IN, live=released).errors
    assert not any("engaged loop" in error for error in released_errors)


@pytest.mark.requirement("AGENT-29")
def test_validation_refuses_an_incoming_deck_with_an_engaged_loop() -> None:
    """[if] the incoming deck has an engaged loop [then] validation refuses the plan and names that deck and its loop, [else stop]."""
    looped = _live()
    looped["decks"]["2"]["loop_engaged"] = True
    errors = validate_plan(parse_plan(_plan()), OUT, IN, live=looped).errors
    assert any("deck 2" in error and "engaged loop" in error for error in errors)
    released = _live()
    released["decks"]["2"]["loop_engaged"] = False
    released_errors = validate_plan(parse_plan(_plan()), OUT, IN, live=released).errors
    assert not any("deck 2" in error and "engaged loop" in error for error in released_errors)


def test_a_folded_launch_carries_its_fold_and_master_to_the_page() -> None:
    """[if] Beat Sync folds the incoming tempo [then] the prepared beat_sync names the fold and its master, [else stop]."""
    live = _live(effective_bpm=128.0)
    live["decks"]["2"]["pitch_range"] = 6.0
    order = compile_plan(parse_plan(_plan()), profile("trk-in", bpm=64.0), plan_id="plan-test", live=live)["plan"]
    sync = next(c for c in order["prepare"] if c["type"] == "beat_sync")
    assert sync == {"type": "beat_sync", "deck": 2, "enabled": True, "normalization": 0.5, "master": 1}


def test_an_unfolded_launch_does_not_name_a_fold_to_the_page() -> None:
    live = _live(effective_bpm=128.0)
    live["decks"]["2"]["pitch_range"] = 6.0
    order = compile_plan(parse_plan(_plan()), profile("trk-in", bpm=128.0), plan_id="plan-test", live=live)["plan"]
    sync = next(c for c in order["prepare"] if c["type"] == "beat_sync")
    assert sync == {"type": "beat_sync", "deck": 2, "enabled": True}
