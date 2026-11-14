"""#11556: the low-band crossover during the overlap (incoming LO parked at 1/3, crossing to flat), never both full.

Pure: a 16-bar parametric crossfade plan built by the real style builder and replayed through the plan simulator.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from apps.autodj.plan import parse_plan, state_at
from apps.autodj.styles import FLAT, PRE_HIT_LOW, StyleParams, crossover_bars, hit_bounds, knob_amp, parametric_builder

#: Mirrors LOW_RULE_EXCURSION_DB in apps/transition_score/scorer.py (not imported: the scorer pulls in scipy).
LOW_RULE_EXCURSION_DB = 1.5

OUT_DECK, IN_DECK, BARS = 1, 2, 16
PARAMS = StyleParams(volume="crossfade")


def _doc(events: list[dict[str, Any]], hit: float) -> dict[str, Any]:
    return {
        "out": {"deck": OUT_DECK, "track": "trk-out", "window": {"start_bar": 80 - BARS, "bars": BARS}},
        "in": {"deck": IN_DECK, "track": "trk-in", "window": {"start_bar": 1, "bars": BARS}},
        "style": "crossfade",
        "events": events,
        "bass_entry": {"in_bar": 33, "entry_plan_bar": hit, "hit_plan_bar": hit, "note": "test"},
        "shape": None,
    }


def _hit() -> float:
    low, high = hit_bounds(BARS, PARAMS.min_body_fade_bars)
    return (low + high) / 2


def _plan(hit: float, extra: list[dict[str, Any]] | None = None) -> Any:
    events = parametric_builder(PARAMS)(BARS, OUT_DECK, IN_DECK, hit)
    return parse_plan(_doc([*events, *(extra or [])], hit))


def _db(knob: float) -> float:
    """The EQ law: 0.5 flat, 0 = -26 dB, linear in dB below flat."""
    return -26.0 * (FLAT - min(knob, FLAT)) / FLAT


def _rows(plan: Any, start: float, end: float) -> list[Any]:
    return [state_at(plan, s / 16, bar=math.floor(s / 16)) for s in range(int(start * 16), int(end * 16) + 1)]


def _both_near_full(plan: Any, start: float, end: float) -> bool:
    return any(_db(r.out_low) > -3.0 and _db(r.in_low) > -3.0 for r in _rows(plan, start, end))


@pytest.mark.requirement("AGENT-40")
def test_incoming_low_parks_at_a_third_then_crosses_to_flat_while_outgoing_falls_to_a_third() -> None:
    """[if] a 16-bar blend overlaps before the bass entry [then] in LO 0.333 then 0.5, out LO 0.5 to 0.333, [else stop]."""
    hit = _hit()
    plan = _plan(hit)
    assert PRE_HIT_LOW == pytest.approx(0.333, abs=1e-3)
    for row in _rows(plan, 0.0, hit - 1 / 16):
        assert row.in_low == pytest.approx(PRE_HIT_LOW), "if the incoming LO moves before its entry the park broke"
    assert state_at(plan, hit - 1 / 16, bar=math.floor(hit)).out_low == pytest.approx(FLAT)
    end = hit + crossover_bars(BARS - hit)
    done = state_at(plan, end, bar=math.floor(end))
    assert done.in_low == pytest.approx(FLAT)
    assert done.out_low <= PRE_HIT_LOW + 1e-6
    tail = state_at(plan, BARS - 0.5, bar=BARS - 1)
    assert tail.out_low < PRE_HIT_LOW - 0.05, "if the outgoing LO holds after the crossover the body fade lost it"


@pytest.mark.requirement("AGENT-40")
def test_no_beat_has_both_lows_near_full_during_the_body_fade() -> None:
    """[if] the body fade runs [then] no step has both decks' lows within 3 dB of flat, [else stop]."""
    hit = _hit()
    assert not _both_near_full(_plan(hit), 0.0, BARS - 1 / 16)  # bar 16 is the handover reset


@pytest.mark.requirement("AGENT-40")
def test_a_plan_holding_both_lows_flat_through_the_overlap_is_caught() -> None:
    """[if] both eq_low are held at 0.5 through the overlap [then] the both-near-full check flags it, [else stop]."""
    hit = _hit()
    held = [{"at_bar": b, "deck": d, "set": {"eq_low": FLAT}} for b in range(BARS) for d in (OUT_DECK, IN_DECK)]
    assert _both_near_full(_plan(hit, held), 0.0, BARS)


@pytest.mark.requirement("AGENT-40")
def test_the_crossover_low_power_stays_within_the_scorer_excursion_of_one_deck() -> None:
    """[if] both decks carry bass in the crossover [then] the low knob power sum is within 1.5 dB of one deck, [else stop]."""
    hit = _hit()
    plan = _plan(hit)
    end = hit + crossover_bars(BARS - hit)
    for row in _rows(plan, hit, end):
        power = knob_amp(row.out_low) ** 2 + knob_amp(row.in_low) ** 2
        assert abs(10 * math.log10(power)) <= LOW_RULE_EXCURSION_DB, f"low power {power:.3f}"
