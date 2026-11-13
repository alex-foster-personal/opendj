"""Dimmer curves, DMX rounding and gamma luminance (issue #14676).

[if] dimmer curve helpers get valid or bad input [then] outputs are bounded and bad input raises, [else stop].
"""

from __future__ import annotations

import itertools
import math

import pytest

from apps.lighting import dimmer_curve as dc

pytestmark = [pytest.mark.requirement("LIGHT-14684")]


def test_curves_endpoints_and_monotonic() -> None:
    """[if] each curve runs over 0..1 [then] 0 maps to 0, 1 to 1 and output never decreases, [else stop]."""
    xs = [i / 1000 for i in range(1001)]
    for name in dc.CURVES:
        assert dc.apply_curve(name, 0.0) == 0.0
        assert dc.apply_curve(name, 1.0) == pytest.approx(1.0)
        ys = [dc.apply_curve(name, x) for x in xs]
        assert all(b >= a for a, b in itertools.pairwise(ys)), name


def test_dmx_rounding_and_bounds() -> None:
    """[if] levels convert to DMX [then] they round to nearest step within 255 / 65535, [else stop]."""
    assert dc.to_dmx8(0.5) == 128
    assert dc.to_dmx8(1.0) == 255
    assert dc.to_dmx8(0.0) == 0
    assert dc.to_dmx8(1 / 255 * 0.49) == 0
    assert dc.to_dmx16(1.0) == 65535
    assert dc.to_dmx16(0.5) == 32768
    for i in range(1001):
        assert 0 <= dc.to_dmx8(i / 1000) <= 255
        assert 0 <= dc.to_dmx16(i / 1000) <= 65535


def test_rgb_luminance_srgb_wcag() -> None:
    """[if] rgb_luminance gets white, black or grey [then] it matches sRGB + WCAG values, [else stop]."""
    assert dc.rgb_luminance((255, 255, 255)) == pytest.approx(1.0)
    assert dc.rgb_luminance((0, 0, 0)) == 0.0
    s = 128 / 255
    lin = ((s + 0.055) / 1.055) ** 2.4
    assert dc.rgb_luminance((128, 128, 128)) == pytest.approx(lin)
    assert dc.rgb_luminance((0, 255, 0)) == pytest.approx(0.7152)


@pytest.mark.parametrize("bad", [-0.01, 1.01, math.nan, math.inf, -math.inf, "x", True])
def test_bad_inputs_raise(bad: object) -> None:
    """[if] a curve or DMX helper gets out-of-range or non-finite input [then] ValueError, [else stop]."""
    for fn in (dc.linear, dc.square_law, dc.s_curve, dc.gamma22, dc.to_dmx8, dc.to_dmx16):
        with pytest.raises(ValueError):
            fn(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        dc.rgb_luminance((256, 0, 0))
    with pytest.raises(ValueError):
        dc.rgb_luminance((math.nan, 0, 0))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        dc.apply_curve("nope", 0.5)
