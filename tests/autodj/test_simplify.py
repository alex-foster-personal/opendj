"""MEMORY-05: a hand mix's noisy control lanes simplify into one clean plan.

[if] a noisy hand crossfade goes in [then] the exact golden plan comes out, [else stop].
[if] a lane only jitters [then] it leaves no event, [else stop].
[if] nothing moved [then] simplify refuses, never an empty plan, [else stop].
[if] an incoming lane is held constant [then] the plan seeds that start even at unity or flat, [else stop].
"""

from __future__ import annotations

import random

import pytest

from apps.autodj.plan import _value_at, parse_plan
from apps.autodj.simplify import (
    BEAT_BARS,
    CAPTURED_STYLE,
    KNOB_TOLERANCE,
    CapturedMix,
    Sample,
    SimplifyError,
    simplify,
)
from apps.autodj.styles import STYLES

pytestmark = pytest.mark.requirement("MEMORY-05")

OUT_DECK, IN_DECK = 1, 2


def _noisy(curve, bars: float, seed: int, noise: float = 0.012, per_bar: int = 2) -> tuple[Sample, ...]:
    """``curve`` sampled at the mirror's rate (two samples a bar at 120 BPM), with hand wobble."""
    rng = random.Random(seed)
    count = int(bars * per_bar)
    return tuple(
        Sample(step / per_bar, min(1.0, max(0.0, curve(step / per_bar) + rng.uniform(-noise, noise))))
        for step in range(count + 1)
    )


def _mix(lanes, *, end_bar: float = 16.0) -> CapturedMix:
    return CapturedMix(
        out_deck=OUT_DECK,
        out_track="out-track",
        out_start_bar=65,
        in_deck=IN_DECK,
        in_track="in-track",
        in_start_bar=9,
        end_bar=end_bar,
        lanes=lanes,
        tempo_mode="sync_to_out",
    )


def _ramp(start: float, stop: float, begin: float, end: float):
    return lambda bar: start + (stop - start) * min(1.0, max(0.0, (bar - begin) / (end - begin)))


def test_noisy_hand_crossfade_simplifies_to_the_golden_plan() -> None:
    plan = simplify(
        _mix(
            {
                (OUT_DECK, "fader"): _noisy(_ramp(1.0, 0.0, 8.0, 16.0), 16, seed=1),
                (IN_DECK, "fader"): _noisy(_ramp(0.0, 1.0, 0.0, 8.0), 16, seed=2),
                (IN_DECK, "eq_low"): _noisy(lambda bar: 0.0 if bar < 8.0 else 0.5, 16, seed=3),
                (OUT_DECK, "eq_mid"): _noisy(lambda _bar: 0.5, 16, seed=4),
            }
        )
    )
    assert plan.to_json() == {
        "out": {"deck": 1, "track": "out-track", "window": {"start_bar": 65, "bars": 16}},
        "in": {
            "deck": 2,
            "track": "in-track",
            "window": {"start_bar": 9, "bars": 16},
            "trim_db": None,
            "trim_reason": None,
        },
        "anchor": "window",
        "style": CAPTURED_STYLE,
        "tempo": {"mode": "sync_to_out"},
        "events": [
            {"at_bar": -0.25, "deck": 2, "set": {"eq_low": 0.0}},
            {"at_bar": -0.25, "deck": 2, "set": {"fader": 0.0}},
            {"at_bar": 0.0, "deck": 2, "do": "play"},
            {"at_bar": 0.0, "deck": 2, "ramp": {"fader": 1.0}, "over_bars": 8.0},
            {"at_bar": 8.0, "deck": 1, "ramp": {"fader": 0.0}, "over_bars": 8.0},
            {"at_bar": 8.0, "deck": 2, "set": {"eq_low": 0.5}},
            {"at_bar": 16.0, "deck": 2, "do": "master"},
            {"at_bar": 16.0, "deck": 1, "do": "stop"},
            {"at_bar": 16.0, "deck": 1, "set": {"eq_low": 0.5}},
            {"at_bar": 16.0, "deck": 1, "set": {"eq_mid": 0.5}},
            {"at_bar": 16.0, "deck": 1, "set": {"eq_high": 0.5}},
            {"at_bar": 16.0, "deck": 1, "set": {"filter": 0.5}},
            {"at_bar": 16.0, "deck": 1, "set": {"fader": 1.0}},
            {"at_bar": 16.0, "deck": 2, "set": {"filter": 0.5}},
        ],
        "intent": None,
        "rationale": "captured from a hand mix",
        "bass_entry": None,
        "shape": None,
    }


def test_the_document_round_trips_through_parse_plan() -> None:
    plan = simplify(_mix({(OUT_DECK, "fader"): _noisy(_ramp(1.0, 0.0, 0.0, 16.0), 16, seed=5)}))
    assert parse_plan(plan.to_json()).to_json() == plan.to_json()


@pytest.mark.parametrize("seed", range(8))
def test_jitter_alone_is_no_move_and_a_real_ramp_beside_it_survives(seed: int) -> None:
    plan = simplify(
        _mix(
            {
                (OUT_DECK, "fader"): _noisy(_ramp(1.0, 0.0, 0.0, 16.0), 16, seed=seed),
                (OUT_DECK, "filter"): _noisy(lambda _bar: 0.5, 16, seed=seed + 100, noise=KNOB_TOLERANCE),
            }
        )
    )
    assert [event.name for event in plan.events if event.deck == OUT_DECK and event.at_bar < 16] == ["fader"]


@pytest.mark.parametrize("seed", range(8))
def test_jitter_away_from_any_rail_is_no_move_either(seed: int) -> None:
    """At 0.7 no rail snap cleans the ends up, so the lane's own range is what says it never moved."""
    plan = simplify(
        _mix(
            {
                (OUT_DECK, "fader"): _noisy(_ramp(1.0, 0.0, 0.0, 16.0), 16, seed=seed),
                (OUT_DECK, "filter"): _noisy(lambda _bar: 0.7, 16, seed=seed + 200, noise=KNOB_TOLERANCE),
            }
        )
    )
    assert [event.name for event in plan.events if event.deck == OUT_DECK and event.at_bar < 16] == ["fader"]


def test_a_two_stage_fade_keeps_both_slopes_within_the_tolerance() -> None:
    curve = lambda bar: 1.0 - 0.3 * bar / 12.0 if bar < 12.0 else 0.7 * (16.0 - bar) / 4.0  # noqa: E731
    plan = simplify(_mix({(OUT_DECK, "fader"): _noisy(curve, 16, seed=7)}))
    lane = [e for e in plan.events if e.deck == OUT_DECK and e.name == "fader" and e.at_bar < 16]
    assert [e.kind for e in lane] == ["ramp", "ramp"]
    for bar in range(16):
        replayed = _value_at(list(lane), OUT_DECK, "fader", float(bar), 1.0)
        assert replayed == pytest.approx(curve(bar), abs=2 * KNOB_TOLERANCE)


def test_every_event_bar_is_snapped_to_a_beat() -> None:
    plan = simplify(_mix({(OUT_DECK, "fader"): _noisy(_ramp(1.0, 0.0, 3.13, 11.37), 16, seed=8, per_bar=3)}))
    for event in plan.events:
        assert event.at_bar / BEAT_BARS == int(event.at_bar / BEAT_BARS)
        assert event.over_bars / BEAT_BARS == int(event.over_bars / BEAT_BARS)


def test_a_step_within_one_sample_becomes_a_set_not_a_ramp() -> None:
    plan = simplify(_mix({(OUT_DECK, "eq_low"): _noisy(lambda bar: 0.5 if bar < 8.0 else 0.0, 16, seed=9)}))
    lane = [e for e in plan.events if e.deck == OUT_DECK and e.name == "eq_low" and e.at_bar < plan.bars]
    assert [(e.kind, e.at_bar, e.value) for e in lane] == [("set", 0.0, 0.0)]
    assert (plan.out.start_bar, plan.bars) == (73, 8)


def test_a_quiet_lead_in_is_cut_and_the_windows_move_with_it() -> None:
    """Cueing in the headphones for 8 bars is not part of the transition: bar 0 is the bar of the first move."""
    plan = simplify(_mix({(IN_DECK, "fader"): _noisy(_ramp(0.0, 1.0, 8.0, 16.0), 16, seed=10)}))
    assert (plan.out.start_bar, plan.in_.start_bar, plan.bars) == (73, 17, 8)
    assert [e.at_bar for e in plan.events if e.name == "fader" and e.deck == IN_DECK] == [-0.25, 0.0]


def test_incoming_trim_collapses_to_one_set_before_it_is_heard() -> None:
    """validate_plan refuses a trim that moves while the deck is heard, so the hand move lands as its end value."""
    plan = simplify(
        _mix(
            {
                (OUT_DECK, "fader"): _noisy(_ramp(1.0, 0.0, 0.0, 16.0), 16, seed=11),
                (IN_DECK, "trim"): _noisy(_ramp(0.5, 0.75, 2.0, 6.0), 16, seed=12),
            }
        )
    )
    trims = [e for e in plan.events if e.name == "trim"]
    assert [(e.kind, e.at_bar) for e in trims] == [("set", -0.25)]
    assert trims[0].value == pytest.approx(0.75, abs=KNOB_TOLERANCE)


def test_crossfader_lane_is_mixer_wide_and_seeded_before_the_launch() -> None:
    plan = simplify(_mix({(None, "crossfader"): _noisy(_ramp(0.0, 1.0, 0.0, 8.0), 16, seed=13)}, end_bar=8.0))
    lane = [(e.kind, e.at_bar, e.deck, e.value) for e in plan.events if e.name == "crossfader"]
    assert lane == [("set", -0.25, None, 0.0), ("ramp", 0.0, None, 1.0)]
    assert plan.bars == 8


def test_the_plan_ends_where_the_outgoing_deck_went_quiet() -> None:
    samples = _noisy(_ramp(1.0, 0.0, 0.0, 8.0), 32, seed=14)
    assert simplify(_mix({(OUT_DECK, "fader"): samples}, end_bar=8.0)).bars == 8


def test_nothing_moved_is_refused() -> None:
    with pytest.raises(SimplifyError, match="no control moved"):
        simplify(_mix({(OUT_DECK, "fader"): _noisy(lambda _bar: 1.0, 16, seed=15)}))


def test_constant_incoming_fader_unity_is_seeded_in_prepare() -> None:
    """Incoming fader held at 1.0 (neutral) still seeds prepare so replay does not inherit a zeroed fader."""
    plan = simplify(
        _mix(
            {
                (OUT_DECK, "fader"): _noisy(_ramp(1.0, 0.0, 0.0, 16.0), 16, seed=20),
                (IN_DECK, "fader"): _noisy(lambda _bar: 1.0, 16, seed=21, noise=KNOB_TOLERANCE * 0.4),
            }
        )
    )
    presets = [e for e in plan.events if e.at_bar < 0 and e.deck == IN_DECK and e.name == "fader"]
    assert len(presets) == 1
    assert presets[0].kind == "set"
    assert presets[0].value == pytest.approx(1.0, abs=KNOB_TOLERANCE)


def test_constant_incoming_eq_flat_and_trim_flat_are_seeded() -> None:
    plan = simplify(
        _mix(
            {
                (OUT_DECK, "fader"): _noisy(_ramp(1.0, 0.0, 0.0, 16.0), 16, seed=22),
                (IN_DECK, "eq_low"): _noisy(lambda _bar: 0.5, 16, seed=23, noise=KNOB_TOLERANCE * 0.4),
                (IN_DECK, "trim"): _noisy(lambda _bar: 0.5, 16, seed=24, noise=KNOB_TOLERANCE * 0.4),
            }
        )
    )
    prepare_bar = min(e.at_bar for e in plan.events)
    flat_presets = [
        e
        for e in plan.events
        if e.at_bar == prepare_bar and e.deck == IN_DECK and e.kind == "set" and e.name in ("eq_low", "trim")
    ]
    by_name = {e.name: e.value for e in flat_presets}
    assert by_name["eq_low"] == pytest.approx(0.5, abs=KNOB_TOLERANCE)
    assert by_name["trim"] == pytest.approx(0.5, abs=KNOB_TOLERANCE)


def test_constant_incoming_non_neutral_value_is_seeded_as_before() -> None:
    plan = simplify(
        _mix(
            {
                (OUT_DECK, "fader"): _noisy(_ramp(1.0, 0.0, 0.0, 16.0), 16, seed=25),
                (IN_DECK, "eq_high"): _noisy(lambda _bar: 0.3, 16, seed=26, noise=KNOB_TOLERANCE * 0.4),
            }
        )
    )
    presets = [e for e in plan.events if e.at_bar < 0 and e.deck == IN_DECK and e.name == "eq_high"]
    assert len(presets) == 1
    assert presets[0].value == pytest.approx(0.3, abs=KNOB_TOLERANCE)


def test_constant_outgoing_lane_is_not_seeded() -> None:
    plan = simplify(
        _mix(
            {
                (OUT_DECK, "fader"): _noisy(_ramp(1.0, 0.0, 0.0, 16.0), 16, seed=27),
                (OUT_DECK, "eq_low"): _noisy(lambda _bar: 0.5, 16, seed=28, noise=KNOB_TOLERANCE * 0.4),
            }
        )
    )
    assert not any(e.at_bar < 0 and e.deck == OUT_DECK for e in plan.events)


def _crossfade_lanes(delay_bars: float) -> dict[tuple[int | None, str], tuple[Sample, ...]]:
    bars = 16
    template = simplify(_mix({(OUT_DECK, "fader"): _noisy(_ramp(1.0, 0.0, 0.0, 16.0), 16, seed=16)}))
    built = parse_plan({**template.to_json(), "events": STYLES["crossfade"].build(bars, OUT_DECK, IN_DECK)})
    timed = [e for e in built.events if e.at_bar < bars]
    lanes: dict[tuple[int | None, str], tuple[Sample, ...]] = {}
    for deck, control in {(e.deck, e.name) for e in built.events if e.kind != "do"}:
        start = 0.0 if (deck, control) == (IN_DECK, "fader") else 1.0 if control == "fader" else 0.5
        lanes[(deck, control)] = _noisy(
            lambda bar, d=deck, c=control, s=start: _value_at(timed, d, c, bar - delay_bars, s),
            bars,
            seed=17,
            noise=0.008,
        )
    return lanes


def test_a_hand_mix_shaped_like_a_catalog_style_takes_its_name() -> None:
    assert simplify(_mix(_crossfade_lanes(0.0))).style == "crossfade"


def test_a_hand_mix_two_bars_behind_a_catalog_style_stays_captured() -> None:
    assert simplify(_mix(_crossfade_lanes(2.0))).style == CAPTURED_STYLE


def test_a_shape_no_style_has_stays_captured() -> None:
    plan = simplify(_mix({(OUT_DECK, "filter"): _noisy(_ramp(0.5, 0.9, 0.0, 16.0), 16, seed=18)}))
    assert plan.style == CAPTURED_STYLE
