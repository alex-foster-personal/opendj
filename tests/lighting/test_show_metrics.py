"""Show quality metrics for planned shows (#9903).

[if] look changes miss boundaries, one colour has entropy, or repeats miscount [then] fail, [else stop].
"""

from __future__ import annotations

import pytest

from apps.lighting.safety import FixtureLight
from apps.lighting.sections import LightSection, SectionSpan, TrackMood
from apps.lighting.show_metrics import (
    change_rate,
    limiter_held_frames,
    opening_looks,
    palette_entropy,
    phrase_alignment,
    repeat_score,
)
from apps.lighting.show_plan import EffectBand, ShowCue

pytestmark = [pytest.mark.requirement("LIGHT-14495")]

BEATS = tuple(i * 0.5 for i in range(64))  # 120 bpm, 32 s


def _span(start_beat: int, end_beat: int, section: LightSection) -> SectionSpan:
    return SectionSpan(start_beat * 500.0, end_beat * 500.0, section, TrackMood.HIGH, section.value)


SECTIONS = [_span(0, 16, LightSection.INTRO), _span(16, 48, LightSection.DROP), _span(48, 64, LightSection.OUTRO)]


def _cue(beat: int, look: str) -> ShowCue:
    return ShowCue(beat, look, EffectBand.MID, 2)


def test_alignment_is_one_when_every_change_is_on_a_boundary() -> None:
    """[if] a plan changes look exactly at every boundary [then] phrase alignment is 1.0, [else stop]."""
    plan = (_cue(0, "a"), _cue(8, "a"), _cue(16, "b"), _cue(48, "c"))
    assert phrase_alignment(plan, BEATS, SECTIONS) == 1.0
    assert phrase_alignment((*plan[:3], _cue(40, "c")), BEATS, SECTIONS) == 2 / 3


def test_single_colour_has_zero_entropy() -> None:
    """[if] a plan shows a single colour [then] palette entropy is 0, [else stop]."""
    plan = (_cue(0, "a"), _cue(16, "b"))
    assert palette_entropy(plan, 64, {"a": (255, 0, 0), "b": (255, 0, 0)}) == 0.0
    assert palette_entropy((_cue(0, "a"), _cue(32, "b")), 64, {"a": (255, 0, 0), "b": (0, 0, 255)}) == 1.0


def test_three_tracks_same_drop_look_repeat_two() -> None:
    """[if] three consecutive tracks open their drop with the same look [then] the repeat score counts 2, [else stop]."""
    tracks = [
        opening_looks((_cue(0, f"intro{i}"), _cue(16, "drop-x"), _cue(48, f"out{i}")), BEATS, SECTIONS)
        for i in range(3)
    ]
    assert repeat_score(tracks) == 2


def test_change_rate_counts_changes_per_minute() -> None:
    """[if] a 32 s plan changes look twice after its opener [then] change rate is 3.75 per minute, [else stop]."""
    plan = (_cue(0, "a"), _cue(16, "b"), _cue(48, "c"))
    assert change_rate(plan, 32.0) == 3.75


def test_limiter_held_frames_counts_vetoed_frames() -> None:
    """[if] a steady planned frame is fed [then] 0 frames are held, and fast flashing gives some, [else stop]."""
    red = FixtureLight((255, 0, 0), 1.0)
    dark = FixtureLight((0, 0, 0), 0.0)
    assert limiter_held_frames((i / 40, {"f": red}) for i in range(80)) == 0
    assert limiter_held_frames((i / 40, {"f": red if i % 2 else dark}) for i in range(80)) > 0
