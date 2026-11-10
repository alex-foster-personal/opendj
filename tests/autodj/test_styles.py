"""#10246 (FXTRANS-11737): the noise_build style.

[if] noise_build is built [then] only fx_noise is used and the riser cuts on the drop bar, [else stop].
"""

from __future__ import annotations

import pytest

from apps.autodj.styles import HIT_ON_EXIT, STYLES, exit_bar, list_styles, style

pytestmark = pytest.mark.requirement("FXTRANS-11737")


def test_noise_build_is_catalogued() -> None:
    """[if] the catalog is read [then] noise_build is available, fx_noise only, 2..8 bars, hit on the drop, [else stop]."""
    s = style("noise_build")
    assert s.available
    assert s.controls == ("fx_noise",)
    assert (s.min_bars, s.max_bars, s.default_bars) == (2, 8, 5)
    assert s.hit == HIT_ON_EXIT
    assert s.tempo_mode == "none"
    assert s.needs_beatmatch is False
    assert "noise_build" in {row["name"] for row in list_styles()}
    assert s.summary_doc()["controls"] == ["fx_noise"]


@pytest.mark.parametrize("bars", range(2, 9))
def test_noise_build_rides_up_cuts_on_the_drop_and_resets(bars: int) -> None:
    """[if] noise_build is built [then] the riser ramps to E, cuts there and fx_reset runs at the handover, [else stop]."""
    drop = exit_bar(bars)
    events = style("noise_build").build(bars, 1, 2)
    assert {"at_bar": 0, "deck": 1, "set": {"fx_noise": {"level": 0.0, "sweep": 0.0}}} in events
    assert {"at_bar": 0, "deck": 1, "ramp": {"fx_noise": {"level": 0.8, "sweep": 0.7}}, "over_bars": drop} in events
    assert {"at_bar": drop, "deck": 1, "set": {"fx_noise": {"level": 0.0, "sweep": 0.0}}} in events
    assert {"at_bar": drop, "deck": 1, "set": {"fader": 0.0}} in events
    assert {"at_bar": drop, "deck": 2, "do": "play"} in events
    resets = [e for e in events if e.get("do") == "fx_reset"]
    assert resets == [{"at_bar": bars, "deck": 1, "do": "fx_reset"}]
    assert resets[0]["at_bar"] >= drop


def test_noise_build_takes_only_the_drop_bar_as_its_hit() -> None:
    """[if] noise_build gets a hit bar [then] only the drop bar E is accepted, [else stop]."""
    s = style("noise_build")
    assert s.build(5, 1, 2, hit_bar=exit_bar(5)) == s.build(5, 1, 2)
    with pytest.raises(ValueError):
        s.build(5, 1, 2, hit_bar=2.0)


def test_no_other_style_uses_fx_noise() -> None:
    """[if] noise_build is added [then] no other style emits fx_noise, [else stop]."""
    for name, s in STYLES.items():
        if name == "noise_build" or not s.available:
            continue
        events = s.build(s.default_bars, 1, 2)
        assert not any("fx_noise" in e.get("set", {}) or "fx_noise" in e.get("ramp", {}) for e in events), name
