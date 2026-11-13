"""Beat-quantised strobe rate under the flash cap (issue #14512).

[if] a strobe division is asked for at a tempo [then] the rate is beat-quantised and never above the flash cap, [else stop].
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from apps.lighting.safety import SafetySettings
from apps.lighting.strobe_sync import (
    StrobeClock,
    division_hz,
    quantise_division,
    strobe_params,
    strobe_rate_hz,
)

pytestmark = [pytest.mark.requirement("LIGHT-14541")]


def test_quarter_at_128_bpm_is_2_133_hz() -> None:
    """[if] 1/4 is asked for at 128 BPM [then] the rate is 2.133 Hz, [else stop]."""
    assert strobe_rate_hz(128.0, Fraction(1, 4)) == pytest.approx(2.1333, abs=1e-3)


def test_eighth_at_174_bpm_steps_down_to_quarter() -> None:
    """[if] 1/8 at 174 BPM exceeds the 3 Hz cap [then] it steps down to 1/4 (2.9 Hz), [else stop]."""
    assert division_hz(174.0, Fraction(1, 8)) > 3.0
    assert quantise_division(174.0, Fraction(1, 8)) == Fraction(1, 4)
    assert strobe_rate_hz(174.0, Fraction(1, 8)) == pytest.approx(2.9)


def test_beat_phase_preserved_across_tempo_change() -> None:
    """[if] tempo changes mid-strobe [then] beat position and strobe phase stay continuous, [else stop]."""
    clock = StrobeClock(bpm=120.0)
    before = clock.beat_at(1.25)
    phase_before = clock.phase(1.25, Fraction(1, 2))
    clock.set_bpm(1.25, 90.0)
    assert clock.beat_at(1.25) == pytest.approx(before)
    assert clock.phase(1.25, Fraction(1, 2)) == pytest.approx(phase_before)
    assert clock.beat_at(1.25 + 60.0 / 90.0) == pytest.approx(before + 1.0)


def test_no_helper_returns_rate_above_flash_cap() -> None:
    """[if] any tempo, division or lowered cap is used [then] no helper returns a rate above the cap, [else stop]."""
    divisions = [Fraction(1, n) for n in (1, 2, 4, 8, 16, 32)]
    for cap in (3.0, 2.0, 0.5, 0.0):
        settings = SafetySettings(flash_limit_per_second=cap)
        for bpm in (60.0, 128.0, 174.0, 240.0, 999.0):
            for d in divisions:
                assert strobe_rate_hz(bpm, d, settings) <= cap + 1e-9
                params = strobe_params(bpm, d, settings)
                assert params is None or params.rate_hz <= cap + 1e-9
