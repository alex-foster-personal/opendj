"""The bass-hit gate (D11), the outgoing body fade law (D12) and lull_then_drop (D13).

Pure: invented profiles from tests/autodj/fixtures, plans built by the real styles and proposer, replayed through the
plan simulator. Every test names what it predicts and fails loud when the law is broken either way.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import pytest

from apps.autodj.plan import SHAPES, PlanError, _Replay, parse_plan, state_at, validate_plan
from apps.autodj.propose import (
    BASS_ENTRY_HOLD_BARS,
    incoming_bass_entry,
    place_hit,
    propose_transition,
)
from apps.autodj.styles import (
    CROSSOVER_MIN_BARS,
    FLAT,
    HIT_GATED,
    PRE_HIT_LOW,
    STYLES,
    crossover_bars,
    exit_bar,
    hit_bounds,
)
from apps.autodj.windows import MixWindow
from tests.autodj.fixtures import CHORUS, DOWN, INTRO, OUTRO, UP, profile

OUT_DECK, IN_DECK = 1, 2
GATED = sorted(name for name, s in STYLES.items() if s.available and s.hit == HIT_GATED)
#: An incoming track whose steady bass starts at its bar 33 (the chorus), after a bass-light intro and build.
LATE_DROP = (
    (INTRO, 16, 0.4, 0.2, False),
    (UP, 16, 0.6, 0.5, False),
    (CHORUS, 32, 1.0, 1.0, False),
    (OUTRO, 16, 0.4, 0.3, False),
)
#: An outgoing track that idles in a breakdown before its outro.
WITH_LULL = (
    (INTRO, 16, 0.4, 0.3, False),
    (CHORUS, 32, 1.0, 1.0, False),
    (DOWN, 16, 0.3, 0.1, False),
    (OUTRO, 16, 0.4, 0.8, False),
)


def _out_window(label: str = "outro", energy: float | None = 0.6, start: int = 49) -> MixWindow:
    return MixWindow("out", start, 16, label, energy, None, None, 0.5, "test")


def _plan_doc(name: str, bars: int, hit: float, in_start: int = 1) -> dict[str, Any]:
    return {
        "out": {"deck": OUT_DECK, "track": "trk-out", "window": {"start_bar": 80 - bars, "bars": bars}},
        "in": {"deck": IN_DECK, "track": "trk-in", "window": {"start_bar": in_start, "bars": bars}},
        "style": name,
        "events": STYLES[name].build(bars, OUT_DECK, IN_DECK, hit),
        "bass_entry": {"in_bar": 33, "entry_plan_bar": hit, "hit_plan_bar": hit, "note": "test"},
        "shape": "lull_then_drop" if name == "lull_then_drop" else None,
    }


def _gives_way(before: Any, now: Any) -> bool:
    return now.out_fader < before.out_fader - 1e-9 or now.out_low < before.out_low - 1e-9


# ----- D11: the incoming bass entry --------------------------------------------------------------------------------


@pytest.mark.requirement("AGENT-37")
def test_the_bass_entry_is_the_first_bar_of_a_steady_bassline_not_one_loud_bar() -> None:
    """[if] the incoming bass jumps for one bar, then settles later [then] the entry is the steady bar, [else stop]."""
    late = profile("trk-in", arrangement=LATE_DROP)
    assert incoming_bass_entry(late, 1) == 33, "if the entry is not the chorus then the threshold moved"
    series = list(late.bar_bass or ())
    series[19] = max(series)  # one loud bar in the build
    spiked = replace(late, bar_bass=tuple(series))
    assert incoming_bass_entry(spiked, 1) == 33, (
        f"if one loud bar counts then the hold rule ({BASS_ENTRY_HOLD_BARS} bars) is gone"
    )
    assert incoming_bass_entry(late, 40) == 40, "if a window inside the bassline gets a later entry then from_bar broke"
    gone = tuple(1.0 if b < 8 else 0.1 for b in range(len(series)))  # the bassline ends before bar 20
    assert incoming_bass_entry(replace(late, bar_bass=gone), 20) is None


@pytest.mark.requirement("AGENT-37")
@pytest.mark.requirement("AGENT-9355")
def test_a_long_silent_run_then_bass_enters_on_the_first_bass_bars() -> None:
    """[if] bar_bass is 120 silent bars then 8 full ones [then] the entry is bar 121, [else stop]."""
    late = profile(
        "trk-in",
        arrangement=((INTRO, 120, 0.4, 0.0, False), (CHORUS, 8, 1.0, 1.0, False)),
    )
    assert incoming_bass_entry(late, 1) == 121


@pytest.mark.requirement("AGENT-37")
@pytest.mark.requirement("AGENT-9355")
def test_an_all_zero_bass_series_has_no_entry() -> None:
    """[if] bar_bass is all zeros [then] incoming_bass_entry returns None, [else stop]."""
    silent = profile("trk-in", arrangement=((INTRO, 64, 0.4, 0.0, False),))
    assert incoming_bass_entry(silent, 1) is None
    refused = place_hit(STYLES["bass_swap_blend"], _out_window(), silent, 1, 16)
    assert refused.refused is not None and "never lands" in refused.refused


# ----- D11: every style gives way only after the hit ---------------------------------------------------------------


@pytest.mark.requirement("AGENT-37")
@pytest.mark.parametrize("name", GATED)
def test_no_gated_style_touches_the_outgoing_deck_before_the_hit(name: str) -> None:
    """[if] a gated blend is built with its hit anywhere in bounds [then] no outgoing fader or low moves before it, [else stop]."""
    style = STYLES[name]
    bars = style.default_bars
    low, high = hit_bounds(bars, style.min_body_fade_bars)
    for hit in sorted({low, high, (low + high) / 2}):
        plan = parse_plan(_plan_doc(name, bars, hit))
        assert validate_plan(plan, profile("trk-out", arrangement=WITH_LULL), profile("trk-in")).errors == [], name
        previous = state_at(plan, 0.0, bar=0)
        for step in range(1, int(hit * 16)):
            now = state_at(plan, step / 16, bar=math.floor(step / 16))
            assert not _gives_way(previous, now), f"{name}: the outgoing gives way at bar {step / 16} before hit {hit}"
            previous = now
        crossed = hit + crossover_bars(bars - hit)
        after = state_at(plan, crossed, bar=math.floor(crossed))
        assert after.out_low < FLAT and after.in_low == pytest.approx(FLAT), f"{name}: no bass crossover after {hit}"


@pytest.mark.requirement("AGENT-37")
def test_validation_refuses_an_outgoing_move_before_the_recorded_hit_and_allows_it_on_the_hit() -> None:
    """[if] an outgoing low kill lands one bar before the plan's hit [then] validation refuses it, [else stop]."""
    document = _plan_doc("bass_swap_blend", 16, 8.0)
    early = {"at_bar": 7.0, "deck": OUT_DECK, "set": {"eq_low": 0.0}}
    refused = validate_plan(
        parse_plan(document | {"events": [*document["events"], early]}), profile("trk-out"), profile("trk-in")
    )
    assert any("before the incoming bass lands at bar 8 (D11)" in error for error in refused.errors)
    on_hit = {"at_bar": 8.0, "deck": OUT_DECK, "set": {"eq_low": 0.0}}
    allowed = validate_plan(
        parse_plan(document | {"events": [*document["events"], on_hit]}), profile("trk-out"), profile("trk-in")
    )
    assert not any("D11" in error for error in allowed.errors), (
        "if a move ON the hit is refused then the gate overshoots"
    )


# ----- D12: the body fade -------------------------------------------------------------------------------------------


@pytest.mark.requirement("AGENT-41")
@pytest.mark.parametrize("name", GATED)
def test_the_incoming_smashes_in_while_the_outgoing_fades_long_inside_one_deck_of_lows(name: str) -> None:
    """[if] a gated blend's hit lands late [then] the incoming smashes in inside one deck of lows, [else stop].

    The incoming is at unity by the dominance switch, the outgoing body fade stays long, and the low band never sums
    above one deck.

    r5: the long late fade is the outgoing's; the incoming's entry is the hit. Low band: correlated kicks sum in
    amplitude, so fader_out * low_out + fader_in * low_in <= 1 on the knob law."""
    from apps.autodj.styles import knob_amp

    style = STYLES[name]
    bars = style.default_bars
    _low, hit = hit_bounds(bars, style.min_body_fade_bars)
    plan = parse_plan(_plan_doc(name, bars, hit))
    pre = state_at(plan, hit - 1 / 64, bar=math.floor(hit - 1 / 64)).in_fader
    assert pre == pytest.approx(10 ** (-6 / 20), abs=2e-2)
    switch = hit + (crossover_bars(bars - hit) if "swap" not in name else 1.0)
    # Round 20: the crossover trim (at most 2 dB) is released as the outgoing low leaves its hold, within a bar.
    at_switch = state_at(plan, switch + 1.0, bar=math.floor(switch + 1.0))
    # r7: unity less the headroom trim, which is under 1 dB once the outgoing mids have given way to -6 dB.
    assert at_switch.in_fader >= 0.89, f"{name}: the incoming is not at full (less headroom) by the dominance switch"
    still_up = [
        s / 16
        for s in range(int(hit * 16), bars * 16)
        if state_at(plan, s / 16, bar=math.floor(s / 16)).out_fader > 1e-3
    ]
    assert still_up[-1] - hit >= style.min_body_fade_bars - 1 / 16 - 1e-9, f"{name}: body fade shorter than its minimum"
    worst = 0.0
    for s in range(int(hit * 32), bars * 32 - 1):
        row = state_at(plan, s / 32, bar=math.floor(s / 32))
        assert row.in_fader >= pre - 1e-6, f"{name}: the incoming dips below its pre-hit level at {s / 32}"
        # Inside the crossover the maintainer's knob positions stand (r7); the outgoing LO then leaves its 1/3 hold within half
        # a bar.
        if s / 32 > switch + 0.5:
            worst = max(worst, row.out_fader * knob_amp(row.out_low) + row.in_fader * knob_amp(row.in_low))
    # 0.05 (0.4 dB): a killed LO is -26 dB on the knob law, not silence, so a still-loud outgoing leaks about 0.03.
    assert worst <= 1.0 + 0.05, f"{name}: the low band sums to {worst:.3f} of one deck"
    # Control: the outgoing is still audible past the switch (the fade is long, not a second cut).
    assert state_at(plan, switch + 1, bar=math.floor(switch + 1)).out_fader > 0.3


# ----- the proposer places the hit ---------------------------------------------------------------------------------


@pytest.mark.requirement("AGENT-37")
def test_a_late_bass_entry_moves_the_in_window_and_says_so() -> None:
    """[if] the incoming bass enters after the latest allowed hit [then] the in-window moves there and says so, [else stop]."""
    late = profile("trk-in", arrangement=LATE_DROP)
    placed = place_hit(STYLES["bass_swap_blend"], _out_window(), late, 1, 16)
    assert placed.refused is None and placed.hit_bar == 8.0
    assert placed.in_start_bar == 33 - 8 and "moved from bar 1" in placed.note
    assert placed.entry is not None
    assert placed.entry == {"in_bar": 33, "entry_plan_bar": 8.0, "hit_plan_bar": 8.0, "note": placed.entry["note"]}
    # Control: a window already inside the bassline keeps its start, holding the incoming lows to the default hit.
    inside = place_hit(STYLES["bass_swap_blend"], _out_window(), late, 33, 16)
    assert inside.entry is not None
    assert inside.in_start_bar == 33 and inside.hit_bar == 8.0 and inside.entry["entry_plan_bar"] == 0.0


@pytest.mark.requirement("AGENT-37")
def test_a_bass_that_never_lands_or_a_missing_series_is_said_never_guessed() -> None:
    """[if] the bass never lands or its series is missing [then] a refusal or an unknown entry is said, [else stop]."""
    flat = profile("trk-in", arrangement=((INTRO, 64, 0.4, 0.2, False),))
    gone = replace(flat, bar_bass=tuple(1.0 if b < 8 else 0.1 for b in range(64)))  # the bassline ends at bar 8
    refused = place_hit(STYLES["bass_swap_blend"], _out_window(), gone, 20, 16)
    assert refused.refused is not None and "never lands" in refused.refused
    unknown = place_hit(STYLES["bass_swap_blend"], _out_window(), replace(flat, bar_bass=None), 1, 16)
    assert unknown.refused is None and unknown.entry is not None
    assert unknown.entry["in_bar"] is None and "unknown" in unknown.entry["note"]


@pytest.mark.requirement("AGENT-37")
def test_a_cut_enters_on_the_incoming_bass() -> None:
    """[if] a cut's in-window starts before the incoming bass [then] it moves onto the entry, [else stop]."""
    placed = place_hit(STYLES["cut"], _out_window(), profile("trk-in", arrangement=LATE_DROP), 1, 1)
    assert placed.in_start_bar == 33 and placed.hit_bar == 0.0


@pytest.mark.requirement("FXTRANS-9850")
def test_echo_out_enters_on_the_incoming_bass_at_its_exit_bar() -> None:
    """[if] echo_out places its hit [then] the incoming starts on its bass at exit bar E, [else stop]."""
    placed = place_hit(STYLES["echo_out"], _out_window(), profile("trk-in", arrangement=LATE_DROP), 1, 3)
    assert placed.in_start_bar == 33
    assert placed.hit_bar == exit_bar(3)
    assert placed.entry is not None
    assert placed.entry["entry_plan_bar"] == placed.entry["hit_plan_bar"] == 2.0
    with pytest.raises(ValueError, match="exit bar"):
        STYLES["echo_out"].build(3, OUT_DECK, IN_DECK, 0.0)


@pytest.mark.requirement("AGENT-37")
def test_every_proposal_records_its_bass_entry_and_refusals_are_listed() -> None:
    """[if] the proposer runs [then] every plan carries bass_entry and every skipped window says why, [else stop]."""
    result = propose_transition(
        profile("trk-out", arrangement=WITH_LULL),
        profile("trk-in", arrangement=LATE_DROP),
        out_deck=1,
        in_deck=2,
        top=None,
    )
    assert result["proposals"], "if nothing is proposed then the gate refuses too much"
    for proposal in result["proposals"]:
        plan = proposal["plan"]
        assert plan["bass_entry"] is not None and plan["bass_entry"]["hit_plan_bar"] is not None, plan["style"]
    assert all(row["reason"] for row in result["not_offered"])


# ----- D13: lull_then_drop ------------------------------------------------------------------------------------------


@pytest.mark.requirement("AGENT-39")
def test_lull_then_drop_is_offered_on_a_lull_and_lands_the_drop_on_the_handover() -> None:
    """[if] the outgoing window is a breakdown with a drop in reach [then] lull_then_drop is offered and smashes, [else stop].

    Offered with its shape set; the outgoing is untouched to the handover and the incoming is at full on it."""
    result = propose_transition(
        profile("trk-out", arrangement=WITH_LULL),
        profile("trk-in", arrangement=LATE_DROP),
        out_deck=1,
        in_deck=2,
        top=None,
    )
    lulls = [p["plan"] for p in result["proposals"] if p["plan"]["style"] == "lull_then_drop"]
    assert lulls, "if no lull_then_drop is offered on a breakdown then D13 is unreachable"
    plan = parse_plan(lulls[0])
    assert plan.shape == "lull_then_drop" and plan.bass_entry is not None
    assert plan.bass_entry.hit_plan_bar == plan.bars and plan.bass_entry.in_bar == 33
    for step in range(plan.bars * 16):
        row = state_at(plan, step / 16, bar=math.floor(step / 16))
        assert row.out_fader == 1.0 and row.out_low == FLAT, f"the outgoing gives way at {step / 16} before the drop"
        assert row.in_low == 0.0
    assert state_at(plan, plan.bars, bar=plan.bars).in_fader == 1.0


@pytest.mark.requirement("AGENT-39")
def test_lull_then_drop_is_refused_without_a_lull_and_a_plan_never_implies_its_shape() -> None:
    """[if] there is no lull, or a plan's shape and style disagree [then] it is refused either way, [else stop]."""
    placed = place_hit(
        STYLES["lull_then_drop"], _out_window("outro", 0.9), profile("trk-in", arrangement=LATE_DROP), 1, 16
    )
    assert placed.refused is not None and "not a lull" in placed.refused
    assert (
        place_hit(
            STYLES["lull_then_drop"], _out_window("breakdown", 0.3), profile("trk-in", arrangement=LATE_DROP), 1, 16
        ).refused
        is None
    )
    out, inc = profile("trk-out", arrangement=WITH_LULL), profile("trk-in", arrangement=LATE_DROP)
    implied = _plan_doc("lull_then_drop", 16, 16.0) | {"shape": None}
    assert any("carries shape" in e for e in validate_plan(parse_plan(implied), out, inc).errors)
    claimed = _plan_doc("bass_swap_blend", 16, 8.0) | {"shape": "lull_then_drop"}
    assert any("carries shape" in e for e in validate_plan(parse_plan(claimed), out, inc).errors)
    with pytest.raises(PlanError, match="shape must be one of"):
        parse_plan(_plan_doc("bass_swap_blend", 16, 8.0) | {"shape": "surprise"})
    assert {"lull_then_drop"} == SHAPES


@pytest.mark.requirement("AGENT-37")
def test_bass_entry_and_shape_survive_the_plan_round_trip() -> None:
    """[if] a plan with bass_entry and shape is serialized and parsed [then] both come back equal, [else stop]."""
    plan = parse_plan(_plan_doc("lull_then_drop", 16, 16.0))
    again = parse_plan(plan.to_json())
    assert again.bass_entry == plan.bass_entry and again.shape == "lull_then_drop"


# ----- review findings on #7818 ------------------------------------------------------------------------------------


@pytest.mark.requirement("AGENT-37")
def test_a_delayed_hit_waits_for_bass_that_is_still_playing() -> None:
    """[if] a short early bass run has ended by the style's own hit [then] the hit moves to the bass that returns, [else stop].

    Codex #7818: four strong bars, twelve light, then a steady bassline; the hit must not open into the light stretch."""
    base = profile("trk-in", arrangement=LATE_DROP)
    series = tuple(1.0 if b < 4 or b >= 16 else 0.2 for b in range(len(base.bar_bass or ())))
    early_run = replace(base, bar_bass=series)
    placed = place_hit(STYLES["bass_swap_blend"], _out_window(), early_run, 1, 16)
    assert placed.refused is None and placed.entry is not None
    assert placed.entry["in_bar"] == 17, (
        "if the entry stays at bar 1 then the hit opens the lows into a bass-light stretch"
    )
    assert placed.in_start_bar + math.floor(placed.hit_bar) >= 17


@pytest.mark.requirement("AGENT-37")
def test_a_folded_beat_sync_lands_the_hit_on_a_fractional_plan_bar() -> None:
    """[if] Beat Sync folds two bars per plan bar on an odd entry bar [then] the hit is the exact half bar, [else stop].

    Codex #7809: rounding up held the lows through the drop's first bar."""
    late = profile("trk-in", arrangement=LATE_DROP)
    placed = place_hit(STYLES["bass_swap_blend"], _out_window(), late, 18, 16, fold=2.0)
    assert placed.entry is not None and placed.entry["entry_plan_bar"] == pytest.approx(7.5)
    assert placed.hit_bar == pytest.approx(8.0)  # the overlap lane's default, already past the entry
    gated = place_hit(STYLES["crossfade"], _out_window(), late, 18, 16, fold=2.0)
    assert gated.hit_bar == pytest.approx(7.5), (
        "if the hit rounds to a whole bar then the drop's first bar loses its lows"
    )


@pytest.mark.requirement("AGENT-37")
def test_a_moved_window_is_scored_as_the_section_it_lands_on_and_never_duplicated() -> None:
    """[if] the in-window moves to the bass entry [then] its label is re-read there and no two plans share one entry, [else stop]."""
    from apps.autodj.windows import window_at

    late = profile("trk-in", arrangement=LATE_DROP)
    result = propose_transition(profile("trk-out", arrangement=WITH_LULL), late, out_deck=1, in_deck=2, top=None)
    keys = [
        (p["plan"]["style"], p["bars"], p["plan"]["out"]["window"]["start_bar"], p["plan"]["in"]["window"]["start_bar"])
        for p in result["proposals"]
    ]
    assert len(keys) == len(set(keys)), "if two plans share one entered window then dedupe keys on the scored bar"
    moved = [p for p in result["proposals"] if "in-window moved" in p["rationale"]]
    assert moved, "if no window moves on a late drop then the case is not exercised"
    for p in moved:
        start = p["plan"]["in"]["window"]["start_bar"]
        assert f"enter at bar {start} ({window_at(late, 'in', start).label})" in p["rationale"]


@pytest.mark.requirement("AGENT-39")
def test_lull_then_drop_fits_inside_the_outgoing_lull_or_is_refused() -> None:
    """[if] the outgoing breakdown is shorter than the overlap [then] lull_then_drop shrinks to it or is refused, [else stop].

    Codex #7818: an 8-bar breakdown before a chorus let the outgoing drop land before the incoming one."""
    short_lull = (
        (INTRO, 16, 0.4, 0.3, False),
        (CHORUS, 32, 1.0, 1.0, False),
        (DOWN, 8, 0.3, 0.1, False),
        (CHORUS, 16, 1.0, 1.0, False),
        (OUTRO, 8, 0.4, 0.8, False),
    )
    out = profile("trk-out", arrangement=short_lull)
    result = propose_transition(out, profile("trk-in", arrangement=LATE_DROP), out_deck=1, in_deck=2, top=None)
    for p in result["proposals"]:
        if p["plan"]["style"] != "lull_then_drop":
            continue
        start, bars = p["plan"]["out"]["window"]["start_bar"], p["plan"]["out"]["window"]["bars"]
        assert start + bars <= 57, (
            f"if the lull plan runs past the breakdown's end (bar 57) then the outgoing drops first: {start}+{bars}"
        )
    tiny = (
        (INTRO, 16, 0.4, 0.3, False),
        (CHORUS, 32, 1.0, 1.0, False),
        (DOWN, 4, 0.3, 0.1, False),
        (CHORUS, 28, 1.0, 1.0, False),
    )
    refused = propose_transition(
        profile("trk-out", arrangement=tiny), profile("trk-in", arrangement=LATE_DROP), out_deck=1, in_deck=2, top=None
    )
    assert not any(p["plan"]["style"] == "lull_then_drop" for p in refused["proposals"])
    assert any("lull" in row["reason"] and row["style"] == "lull_then_drop" for row in refused["not_offered"])


@pytest.mark.requirement("AGENT-39")
def test_a_folded_lull_lands_the_drop_exactly_on_the_handover() -> None:
    """[if] Beat Sync folds half an incoming bar per plan bar [then] a lull plan's drop is exactly on its handover, [else stop].

    Codex #7818: a 9-bar overlap under a 0.5 fold put the drop one plan bar before the smash."""
    late = profile("trk-in", arrangement=LATE_DROP)
    odd = place_hit(STYLES["lull_then_drop"], _out_window("breakdown", 0.3), late, 1, 9, fold=0.5)
    assert odd.refused is not None and "fold" in odd.refused
    even = place_hit(STYLES["lull_then_drop"], _out_window("breakdown", 0.3), late, 1, 8, fold=0.5)
    assert even.refused is None and even.entry is not None
    assert even.entry["entry_plan_bar"] == even.hit_bar == 8.0


# ----- D14: the low crossover (the maintainer's 1/3-turn rule) -----------------------------------------------------------------
#: Styles whose point is a killed or filtered entry keep the incoming lows killed before the hit.
KILLED_ENTRY = {"filter_sweep", "melt"}
#: Styles that swap the lows ON the hit instead of crossing them over (D14: a first-class choice, not the default).
SWAPS = {"bass_swap_blend"}
CROSSOVERS = sorted(set(GATED) - SWAPS)


def _low_db(knob: float) -> float:
    """The EQ law: 0.5 flat, 0 = -26 dB, linear in dB below flat."""
    return -26.0 * (FLAT - min(knob, FLAT)) / FLAT


@pytest.mark.requirement("AGENT-40")
@pytest.mark.parametrize("name", CROSSOVERS)
def test_the_incoming_lows_wait_a_third_down_and_cross_over_without_both_near_full(name: str) -> None:
    """[if] a gated blend runs [then] its lows cross over from a third down and are never both near full, [else stop].

    Before the hit the incoming LO sits at 1/3 knob (KILL for a killed entry); for the crossover the outgoing LO falls to
    1/3 or lower while the incoming rises to flat, and no point has both lows within 3 dB of flat."""
    style = STYLES[name]
    bars = style.default_bars
    low, high = hit_bounds(bars, style.min_body_fade_bars)
    hit = (low + high) / 2
    plan = parse_plan(_plan_doc(name, bars, hit))
    expected = 0.0 if name in KILLED_ENTRY else PRE_HIT_LOW
    assert state_at(plan, hit - 0.5, bar=math.floor(hit - 0.5)).in_low == pytest.approx(expected), name
    cross = crossover_bars(bars - hit)
    assert cross >= CROSSOVER_MIN_BARS
    for step in range(int(hit * 16), int((hit + cross) * 16) + 1):
        row = state_at(plan, step / 16, bar=math.floor(step / 16))
        near_full = _low_db(row.out_low) > -3.0 and _low_db(row.in_low) > -3.0
        assert not near_full, f"{name}: both lows near full at bar {step / 16}"
    # r5: the outgoing LO may sit below the plain crossover where the faders leave no room (the low-band law).
    end = state_at(plan, hit + cross, bar=math.floor(hit + cross))
    assert end.out_low <= PRE_HIT_LOW + 1e-6 and end.in_low == pytest.approx(FLAT), name


@pytest.mark.requirement("AGENT-40")
def test_a_partial_low_crossover_is_no_bassline_clash_but_two_full_lows_are() -> None:
    """[if] both lows are partly present [then] validation counts no clash; two lows near full still count, [else stop]."""
    out, inc = profile("trk-out"), profile("trk-in")  # both windows inside a chorus: strong bass on both tracks
    crossover = _plan_doc("bass_swap_blend", 16, 8.0)
    crossover["out"]["window"]["start_bar"], crossover["in"]["window"]["start_bar"] = 40, 33
    assert validate_plan(parse_plan(crossover), out, inc).metrics["bass_overlap_bars"] == 0
    both_full = {**crossover, "events": list(crossover["events"])}
    both_full["events"] = [*both_full["events"], {"at_bar": 0, "deck": IN_DECK, "set": {"eq_low": FLAT}}]
    both_full["bass_entry"] = None  # the gate is not under test here; only the clash count
    assert validate_plan(parse_plan(both_full), out, inc).metrics["bass_overlap_bars"] > 0, (
        "if two full basslines are not counted then the clash rule overshoots"
    )


@pytest.mark.requirement("AGENT-40")
@pytest.mark.parametrize("name", sorted(SWAPS))
def test_the_bass_swap_stays_a_hard_swap_on_the_hit(name: str) -> None:
    """[if] a swap style runs [then] its incoming lows are killed until the hit and open ON it, [else stop].

    The outgoing lows are gone a bar later (D14: the swap stays a first-class choice beside the crossover)."""
    from apps.autodj.styles import SWAP_EDGE_BARS

    style = STYLES[name]
    bars = style.default_bars
    low, high = hit_bounds(bars, style.min_body_fade_bars)
    hit = (low + high) / 2
    plan = parse_plan(_plan_doc(name, bars, hit))
    assert state_at(plan, hit - 0.25, bar=math.floor(hit - 0.25)).in_low == 0.0
    assert state_at(plan, hit, bar=math.floor(hit)).in_low == pytest.approx(FLAT)
    gone = hit + SWAP_EDGE_BARS
    assert state_at(plan, gone, bar=math.floor(gone)).out_low == pytest.approx(0.0)


@pytest.mark.requirement("AGENT-42")
@pytest.mark.parametrize("name", sorted(SWAPS))
def test_a_swap_lands_its_hit_at_full_and_starts_the_fade_after_it(name: str) -> None:
    """[if] a swap style runs [then] its hit lands at full before the outgoing fades, [else stop].

    The incoming is at unity ON the hit and the outgoing fader holds through the swap edge, so the fade never muffles
    the hit (the maintainer, p10)."""
    from apps.autodj.styles import SWAP_EDGE_BARS

    style = STYLES[name]
    bars = style.default_bars
    low, high = hit_bounds(bars, style.min_body_fade_bars)
    hit = (low + high) / 2
    plan = parse_plan(_plan_doc(name, bars, hit))
    on_hit = state_at(plan, hit, bar=math.floor(hit))
    assert on_hit.in_fader >= 0.89, f"{name}: the incoming is not at full (less headroom) on the hit"
    edge = state_at(plan, hit + SWAP_EDGE_BARS - 1 / 32, bar=math.floor(hit + SWAP_EDGE_BARS - 1 / 32))
    assert edge.out_fader == pytest.approx(on_hit.out_fader, abs=1e-6), f"{name}: the outgoing faded across the hit"
    # Control: the outgoing does fade after the edge (the hold is not a stuck fader).
    later = hit + SWAP_EDGE_BARS + 2
    assert state_at(plan, later, bar=math.floor(later)).out_fader < on_hit.out_fader - 0.05


@pytest.mark.requirement("AGENT-44")
@pytest.mark.parametrize("name", GATED)
def test_the_two_bodies_never_sum_above_one_deck_after_the_hit(name: str) -> None:
    """[if] a gated blend runs past its hit [then] the two bodies stay within one deck in power, [else stop].

    Incoming fader squared plus (outgoing fader times its mid EQ) squared; the outgoing mids never drop below -6 dB
    (r7: nothing clips by construction)."""
    from apps.autodj.styles import knob_amp

    style = STYLES[name]
    bars = style.default_bars
    low, high = hit_bounds(bars, style.min_body_fade_bars)
    hit = (low + high) / 2
    plan = parse_plan(_plan_doc(name, bars, hit))
    for s in range(int(hit * 32) + 1, bars * 32 - 1):
        mid = _Replay(list(plan.events)).value(OUT_DECK, "eq_mid", s / 32, FLAT)
        row = state_at(plan, s / 32, bar=math.floor(s / 32))
        power = row.in_fader**2 + (row.out_fader * knob_amp(mid)) ** 2
        assert power <= 1.0 + 0.03, f"{name}: bodies sum to {power:.3f} of one deck at {s / 32}"
        assert knob_amp(mid) >= 0.5 - 0.02, f"{name}: the outgoing mids drop below -6 dB at {s / 32}"


@pytest.mark.requirement("AGENT-45")
@pytest.mark.parametrize("name", CROSSOVERS)
def test_the_outgoing_lows_hold_a_third_down_until_the_incoming_is_flat(name: str) -> None:
    """[if] a crossover runs [then] the outgoing LO holds 1/3 until the incoming LO is flat, [else stop].

    Never killed on the hit (the maintainer's rule; r7 p15: two bars with nobody carrying the bass)."""
    if name in KILLED_ENTRY:
        pytest.skip("a killed entry crosses from KILL, not from the park")
    style = STYLES[name]
    bars = style.default_bars
    low, high = hit_bounds(bars, style.min_body_fade_bars)
    hit = (low + high) / 2
    plan = parse_plan(_plan_doc(name, bars, hit))
    cross = crossover_bars(bars - hit)
    for s in range(int(hit * 32), int((hit + cross) * 32) + 1):
        row = state_at(plan, s / 32, bar=math.floor(s / 32))
        assert row.out_low >= PRE_HIT_LOW - 1e-6, f"{name}: outgoing LO {row.out_low:.3f} below 1/3 at {s / 32}"
    assert state_at(plan, hit + cross, bar=math.floor(hit + cross)).in_low == pytest.approx(FLAT)
    # Control: after the crossover the outgoing LO does fall away (the hold is not a stuck knob).
    end = bars - 0.25
    assert state_at(plan, end, bar=math.floor(end)).out_low < PRE_HIT_LOW - 0.1


@pytest.mark.requirement("AGENT-46")
@pytest.mark.parametrize("name", CROSSOVERS)
def test_the_crossover_lows_stay_at_one_deck_unless_the_trim_cap_binds(name: str) -> None:
    """[if] a crossover runs [then] its two lows stay at one deck in amplitude, else the trim is at its cap, [else stop].

    Round 20: the maintainer's knob positions stand and the incoming fader gives up at most CROSSOVER_TRIM_DB, never going under
    its pre-hit level, to bound the lows, since beatmatched kicks add coherently."""
    from apps.autodj.styles import CROSSOVER_TRIM_FLOOR, knob_amp

    if name in KILLED_ENTRY:
        pytest.skip("a killed entry crosses from KILL, not from the park")
    style = STYLES[name]
    bars = style.default_bars
    low, high = hit_bounds(bars, style.min_body_fade_bars)
    hit = (low + high) / 2
    plan = parse_plan(_plan_doc(name, bars, hit))
    cross = crossover_bars(bars - hit)
    pre = 10 ** (-6 / 20)
    capped = 0
    for s in range(int(hit * 64), int((hit + cross) * 64) + 1):
        row = state_at(plan, s / 64, bar=math.floor(s / 64))
        lows = row.out_fader * knob_amp(row.out_low) + row.in_fader * knob_amp(row.in_low)
        untrimmed = pre + (1.0 - pre) * min(1.0, (s / 64 - hit) / cross)
        at_cap = row.in_fader <= max(pre, untrimmed * CROSSOVER_TRIM_FLOOR) + 0.02
        capped += at_cap
        # 0.05: the -26 dB knob floor and the straight ramps between breakpoints.
        assert lows <= 1.0 + 0.05 or at_cap, f"{name}: lows {lows:.3f} with the trim under its cap at {s / 64}"
    # Control: the trim is not always at its cap (it bounds the lows, it is not a blanket 2 dB cut).
    assert capped < (cross * 64) / 2, f"{name}: the trim sits at its cap for most of the crossover"
