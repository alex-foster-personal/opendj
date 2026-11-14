"""#11581: the crossover lows cross equal-power in the band and a linear fade never dips the incoming under pre-hit."""

from __future__ import annotations

import math

import pytest

from apps.autodj.styles import (
    CROSSOVER_TRIM_FLOOR,
    HANDOFF_CROSSOVER,
    _after_hit,
    _crossover_at,
    _out_band,
    _out_fader_at,
    crossover_bars,
)

SPAN, HIT, PRE = 12.0, 8.0, 10 ** (-6 / 20)


def _in_fader_points(shape: str, pre: float) -> list[tuple[float, float]]:
    """Every (bar after the hit, value) the incoming fader is set or ramped to after the hit."""
    points = []
    for event in _after_hit(HANDOFF_CROSSOVER, HIT, SPAN, pre, shape, 1, 2):
        if event.get("deck") != 2:
            continue
        if "fader" in event.get("set", {}):
            points.append((event["at_bar"] - HIT, event["set"]["fader"]))
        if "fader" in event.get("ramp", {}):
            points.append((event["at_bar"] + event["over_bars"] - HIT, event["ramp"]["fader"]))
    return points


@pytest.mark.requirement("TRANSAI-14547")
def test_the_lows_sum_to_one_in_power_at_the_crossover_midpoint() -> None:
    """[if] a crossover is sampled at its midpoint [then] in_low^2 + out_low^2 is 1.0 within 0.03, [else stop]."""
    cross = crossover_bars(SPAN)
    _f_in, in_amp, out_amp = _crossover_at(cross / 2, cross, SPAN - cross, PRE)
    assert in_amp**2 + out_amp**2 == pytest.approx(1.0, abs=0.03)


@pytest.mark.parametrize("pre", [PRE, 0.9])
@pytest.mark.requirement("TRANSAI-14547")
def test_a_linear_fade_never_drops_the_incoming_under_pre(pre: float) -> None:
    """[if] a linear gated blend is sampled after the hit [then] the incoming fader is never under pre, [else stop]."""
    for bar, value in _in_fader_points("linear", pre):
        assert value >= pre - 1e-6, f"incoming fader {value:.3f} under pre {pre:.3f} at {bar}"


@pytest.mark.parametrize("shape", ["equal_power", "linear", "smooth"])
@pytest.mark.requirement("TRANSAI-14547")
def test_the_crossover_trim_never_goes_past_its_floor(shape: str) -> None:
    """[if] the round-20 trim binds in the crossover [then] the incoming stays at max(pre, f_in * floor), [else stop]."""
    cross = crossover_bars(SPAN)
    for bar, value in _in_fader_points(shape, PRE):
        if bar <= cross:
            f_in, f_out = _crossover_at(bar, cross, SPAN - cross, PRE)[0], _out_fader_at(bar, SPAN, 0.0, PRE, shape)
            # The r7 body-power headroom cap comes first; the round-20 trim works on what it leaves.
            untrimmed = min(f_in, math.sqrt(max(0.0, 1.0 - (f_out * _out_band(f_in, f_out)) ** 2)))
            assert value >= max(PRE, untrimmed * CROSSOVER_TRIM_FLOOR) - 0.02, f"{shape}: trim past floor at {bar}"


@pytest.mark.requirement("TRANSAI-14547")
def test_no_sixteenth_has_both_lows_within_3_db_of_flat() -> None:
    """[if] both lows are within 3 dB of flat at a 1/16-bar sample in the crossover [then] it fails, [else stop]."""
    cross = crossover_bars(SPAN)
    near = 10 ** (-3 / 20)
    for step in range(int(cross * 16) + 1):
        _f_in, in_amp, out_amp = _crossover_at(step / 16, cross, SPAN - cross, PRE)
        assert not (in_amp > near and out_amp > near), f"both lows near flat at {step / 16}"
    assert math.isclose(_crossover_at(cross, cross, SPAN - cross, PRE)[1], 1.0)
