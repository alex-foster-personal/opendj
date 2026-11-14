"""#11559 acceptance 3: a gated blend sizes its overlap on the hit plus the body fade, not on the in-window end."""

from __future__ import annotations

from apps.autodj.propose import _overlap_bars, _overlap_skip_reason
from apps.autodj.styles import HIT_GATED, hit_bounds, style
from apps.autodj.windows import MixWindow

OUT = MixWindow("out", 33, 32, "outro", 0.6, None, None, 0.5, "test")
# Pinned: the 16-bar forward-mixability floor is a separate gate; this pins the sizing alone.
IN_TO_CHORUS = MixWindow("in", 1, 8, "intro", 0.4, None, 9, 0.5, "8 bars to the chorus", pinned=True)


def test_gated_blend_overlap_covers_hit_plus_body_fade() -> None:
    """[if] the in window is 8 bars to the chorus and gated blends need 12 bars [then] the overlap fits hit + fade, [else stop]."""
    for name in ("bass_swap_blend", "filter_sweep"):
        gated = style(name)
        assert gated.hit == HIT_GATED and gated.min_bars >= 12
        bars = _overlap_bars(gated, 16, OUT, IN_TO_CHORUS, 128)
        assert bars is not None, _overlap_skip_reason(gated, 16, OUT, IN_TO_CHORUS, 128)
        assert bars >= IN_TO_CHORUS.max_bars + gated.min_body_fade_bars
        low, high = hit_bounds(bars, gated.min_body_fade_bars)
        assert low <= IN_TO_CHORUS.max_bars <= high


def test_short_in_window_still_refuses_non_gated_long_styles() -> None:
    """[if] a style is not gated [then] the 8-bar in window still caps it as before, [else stop]."""
    echo = style("echo_out")
    assert echo.hit != HIT_GATED
    bars = _overlap_bars(echo, 16, OUT, IN_TO_CHORUS, 128)
    assert bars is None or bars <= IN_TO_CHORUS.max_bars
