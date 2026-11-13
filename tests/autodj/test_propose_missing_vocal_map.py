"""AutoDJ proposals with a missing vocal map: unscored, never a synthetic clash (spec d27ed712, issue #13344).

Invented tracks only (tests/autodj/fixtures.py). Missing vocal analysis must stay ``None`` with the vocals term
unscored, so it never reads as measured overlap and never blocks an otherwise viable long blend.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from apps.autodj.profile import TrackProfile
from apps.autodj.propose import propose_transition
from apps.autodj.scoring import score_windows
from apps.autodj.styles import STYLES
from apps.autodj.windows import MixWindow, mix_windows
from tests.autodj.fixtures import profile

OUT, IN = profile("trk-out"), profile("trk-in", key="9A")
MISSING_VOCALS = ("vocals", "a vocal map is missing")


def _pair_with_missing(missing: str) -> tuple[TrackProfile, TrackProfile]:
    """Local copies of the invented pair with the named side's vocal map removed."""
    out_profile = replace(OUT, bar_vocals=None) if missing in {"out", "both"} else OUT
    in_profile = replace(IN, bar_vocals=None) if missing in {"in", "both"} else IN
    return out_profile, in_profile


def _window(track: TrackProfile, role: str, start_bar: int) -> MixWindow:
    """The real mix window of ``role`` that starts at ``start_bar``."""
    return next(window for window in mix_windows(track, role, limit=5) if window.start_bar == start_bar)


def _long_blends(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Proposed styles that default to at least eight bars, the ones a vocal-clash gate would judge."""
    return [row for row in result["proposals"] if STYLES[row["style"]].default_bars >= 8]


def _clash_refusals(result: dict[str, Any]) -> list[str]:
    """Every refusal or rejection reason that names a clash."""
    rows = result["not_offered"] + result["rejected"]
    return [row["reason"] for row in rows if "clash" in row["reason"].lower()]


@pytest.mark.requirement("AGENT-33")
@pytest.mark.parametrize("missing", ["out", "in", "both"])
def test_a_missing_vocal_map_scores_none_not_a_synthetic_clash(missing: str) -> None:
    """[if] a vocal map is absent [then] the clash count is None and vocals are unscored, [else stop]."""
    out_profile, in_profile = _pair_with_missing(missing)
    out_window, in_window = _window(OUT, "out", 65), _window(IN, "in", 1)
    assert out_window.max_bars >= 16 and in_window.max_bars >= 16
    pair = score_windows(out_profile, in_profile, out_window, in_window, overlap_bars=16)
    assert pair.vocal_clash_bars is None
    assert MISSING_VOCALS in pair.unscored
    assert pair.summary()["vocal_clash_bars"] is None
    # Control: with both maps measured the same windows count a real zero, which is scored, not unknown.
    measured = score_windows(OUT, IN, out_window, in_window, overlap_bars=16)
    assert measured.vocal_clash_bars == 0
    assert "vocals" not in dict(measured.unscored)
    assert measured.measured == pair.measured + 1


@pytest.mark.requirement("AGENT-33")
@pytest.mark.parametrize("missing", ["out", "in", "both"])
def test_a_missing_vocal_map_never_blocks_a_viable_blend(missing: str) -> None:
    """[if] a vocal map is absent [then] valid long blends return with no clash refusal, [else stop]."""
    out_profile, in_profile = _pair_with_missing(missing)
    result = propose_transition(out_profile, in_profile, out_deck=1, in_deck=2, intent="smooth", top=None)
    blends = _long_blends(result)
    assert any(row["style"] == "bass_swap_blend" and row["bars"] >= 16 for row in blends)
    assert all(row["validation"]["ok"] for row in blends)
    for row in result["proposals"]:
        assert row["pair"]["vocal_clash_bars"] is None
        assert {"term": MISSING_VOCALS[0], "reason": MISSING_VOCALS[1]} in row["pair"]["unscored"]
    assert _clash_refusals(result) == []


@pytest.mark.requirement("AGENT-33")
def test_an_unscored_viable_long_blend_is_proposed_in_the_top_three() -> None:
    """[if] both vocal maps are absent [then] an unscored long blend still leads the default top three, [else stop]."""
    out_profile, in_profile = _pair_with_missing("both")
    result = propose_transition(out_profile, in_profile, out_deck=1, in_deck=2, intent="smooth")
    assert len(result["proposals"]) == 3
    lead = result["proposals"][0]
    assert STYLES[lead["style"]].default_bars >= 16 and lead["bars"] >= 16
    assert lead["validation"]["ok"]
    assert lead["pair"]["vocal_clash_bars"] is None
    assert {"term": MISSING_VOCALS[0], "reason": MISSING_VOCALS[1]} in lead["pair"]["unscored"]
    assert _clash_refusals(result) == []
