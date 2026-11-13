"""Tests for apps.lighting.colour_temp.

[if] a colour temperature is converted or mixed [then] RGB and white levels follow the approximation, [else stop].
"""

from __future__ import annotations

import itertools
import math

import pytest

from apps.lighting.colour_temp import kelvin_to_rgb, mix_white

pytestmark = [pytest.mark.requirement("LIGHT-14703")]


def test_kelvin_to_rgb_follows_tanner_helland_shape() -> None:
    """[if] 6500 K and 2700 K are converted [then] 6500 K is near white and 2700 K is warm, [else stop]."""
    r, g, b = kelvin_to_rgb(6500)
    assert min(r, g, b) > 0.9
    wr, _wg, wb = kelvin_to_rgb(2700)
    assert wr > wb
    assert wr == pytest.approx(1.0)
    assert kelvin_to_rgb(1000)[2] == 0.0


def test_kelvin_to_rgb_clamped_and_blue_monotonic() -> None:
    """[if] 1000..12000 K is swept [then] channels stay in 0..1 and blue never falls, [else stop]."""
    samples = [kelvin_to_rgb(k) for k in range(1000, 12001, 100)]
    for rgb in samples:
        assert all(0.0 <= c <= 1.0 for c in rgb)
    for a, b in itertools.pairwise(samples):
        assert b[2] >= a[2]


def test_mix_white_hits_target_and_clamps_ends() -> None:
    """[if] a target between warm and cool whites is mixed [then] shares blend and clamp at ends, [else stop]."""
    assert mix_white(2700, 2700, 6500) == (1.0, 0.0)
    assert mix_white(2000, 2700, 6500) == (1.0, 0.0)
    assert mix_white(6500, 2700, 6500) == (0.0, 1.0)
    assert mix_white(9000, 2700, 6500) == (0.0, 1.0)
    warm, cool = mix_white(4000, 2700, 6500)
    assert warm + cool == pytest.approx(1.0)
    assert 0.0 < cool < 1.0
    mixed_mired = warm * (1e6 / 2700) + cool * (1e6 / 6500)
    assert 1e6 / mixed_mired == pytest.approx(4000)
    assert mix_white(5000, 2700, 6500)[1] > cool


@pytest.mark.parametrize("bad", [999, 40001, math.nan, math.inf, -math.inf, 0])
def test_out_of_range_or_non_finite_raises(bad: float) -> None:
    """[if] a temperature is outside 1000..40000 K or non-finite [then] ValueError is raised, [else stop]."""
    with pytest.raises(ValueError):
        kelvin_to_rgb(bad)
    with pytest.raises(ValueError):
        mix_white(bad, 2700, 6500)
