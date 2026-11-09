"""LIGHT-9945: section resolver maps PSSI phrases and own sections onto the lighting vocabulary.

[if] a phrase or section maps to the wrong lighting section, or a bad payload is not refused [then] fail, [else stop].
[if] a PSSI Up-mood chorus is not chorus/high, an own buildup is not build, a payload with no
phrases errors or is not one unknown span, a boundary crossing yields other than one change, or an
unknown PSSI kind is guessed [then] fail, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.lighting.sections import (
    LightSection,
    TrackMood,
    resolve_sections,
    section_events,
    track_mood,
)

pytestmark = [pytest.mark.requirement("LIGHT-9945")]

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def test_pssi_high_mood_chorus_resolves_to_chorus_with_mood_high() -> None:
    """[if] a PSSI High (Up) mood chorus is not chorus with mood high [then] fail."""
    spans = resolve_sections(_load("anlz_pssi_sample.json"))
    chorus = spans[2]
    assert (chorus.section, chorus.mood, chorus.raw) == (LightSection.CHORUS, TrackMood.HIGH, "Chorus")
    assert [span.section for span in spans] == [
        LightSection.INTRO,
        LightSection.BUILD,
        LightSection.CHORUS,
        LightSection.OUTRO,
    ]
    assert (chorus.start_ms, chorus.end_ms) == (30000.0, 60000.0)


def test_own_sections_buildup_resolves_to_build() -> None:
    """[if] an own-sections buildup does not resolve to build [then] fail."""
    spans = resolve_sections(_load("anlz_own_sections_sample.json"))
    assert spans[1].section is LightSection.BUILD
    assert spans[2].section is LightSection.DROP
    assert all(span.mood is TrackMood.UNKNOWN for span in spans)


def test_no_phrases_is_one_unknown_span_over_the_track() -> None:
    """[if] a payload without phrases errors or is not one unknown span [then] fail."""
    assert resolve_sections({"phrases": []}, duration_s=200.0) == resolve_sections({}, duration_s=200.0)
    (span,) = resolve_sections({}, duration_s=200.0)
    assert (span.section, span.start_ms, span.end_ms) == (LightSection.UNKNOWN, 0.0, 200000.0)
    (open_span,) = resolve_sections({"phrases": None})
    assert open_span.section is LightSection.UNKNOWN and open_span.end_ms == float("inf")


def test_window_crossing_one_boundary_gives_exactly_one_change() -> None:
    """[if] a window over one boundary gives other than one change at that boundary [then] fail."""
    spans = resolve_sections(_load("anlz_pssi_sample.json"))
    (change,) = section_events(spans, from_ms=25000.0, to_ms=35000.0)
    assert change.at_ms == 30000.0
    assert (change.previous, change.section) == (LightSection.BUILD, LightSection.CHORUS)
    assert section_events(spans, from_ms=31000.0, to_ms=59000.0) == []


def test_unknown_pssi_kind_maps_to_unknown_never_a_guess() -> None:
    """[if] an unknown PSSI kind or mood maps to a real section [then] fail."""
    (span,) = resolve_sections({"phrases": [{"start_s": 0.0, "end_s": 8.0, "kind": 4, "mood": 1}]})
    assert span.section is LightSection.UNKNOWN
    assert span.mood is TrackMood.HIGH
    (bad_mood,) = resolve_sections({"phrases": [{"start_s": 0.0, "end_s": 8.0, "kind": 1, "mood": 9}]})
    assert (bad_mood.section, bad_mood.mood) == (LightSection.UNKNOWN, TrackMood.UNKNOWN)
    assert track_mood(None) is TrackMood.UNKNOWN
