"""Engine feed attaches the loaded track's lighting profile to each deck's DeckFeed (#14148).

[if] a deck loads, unloads or has no analysis [then] its DeckFeed profile follows it, [else stop].
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.autodj.profile import profile_from_payloads
from apps.lighting.engine_feed import DeckFeedBuilder, analysis_from_anlz, parse_state_line
from tests.autodj.fixtures import anlz_payload

pytestmark = pytest.mark.requirement("LIGHT-14214")

TRACK = "fixture-track-1"
DURATION_MS = 128 * 4 * 60_000.0 / 128.0


def _line(seq: int, *, loaded: bool) -> dict[str, Any]:
    deck = {
        "deck": 1,
        "loaded": loaded,
        "position_ms": 1000.0,
        "rate": 1.0,
        "playing": loaded,
        "duration_ms": DURATION_MS if loaded else 0.0,
        "tempo": 128.0,
        "fader": 1.0,
        "assign": "thru",
    }
    return {
        "type": "state",
        "state_seq": seq,
        "frame": seq,
        "heard_in_ns": 0,
        "decks": [deck],
        "mixer": {"crossfader": 0.0, "master_volume": 1.0},
        "master": {"rms": 0.1, "muted": False},
    }


def _feeds(builder: DeckFeedBuilder, seq: int, *, loaded: bool, analyses: dict[str, Any]) -> Any:
    batch = parse_state_line(_line(seq, loaded=loaded), seq, {"1": TRACK if loaded else None}, analyses)
    return builder.feeds(batch)


def test_loaded_deck_carries_profile_sections() -> None:
    """[if] deck 1 loads a track whose analysis has sections [then] its DeckFeed profile has them, [else stop]."""
    analysis = analysis_from_anlz(TRACK, anlz_payload(TRACK), DURATION_MS, profile_from_payloads)
    (feed,) = _feeds(DeckFeedBuilder(), 1, loaded=True, analyses={TRACK: analysis})
    assert feed.sample.deck == "1"
    assert feed.profile is not None
    assert feed.profile.sections
    assert [s.start_bar for s in feed.profile.sections] == [1, 17, 33, 65, 81, 97, 113]


def test_unloaded_deck_profile_is_none() -> None:
    """[if] deck 1 unloads [then] the next batch has a deck 1 DeckFeed with profile None, [else stop]."""
    builder = DeckFeedBuilder()
    analysis = analysis_from_anlz(TRACK, anlz_payload(TRACK), DURATION_MS, profile_from_payloads)
    (loaded,) = _feeds(builder, 1, loaded=True, analyses={TRACK: analysis})
    assert loaded.profile is not None
    (cleared,) = _feeds(builder, 2, loaded=False, analyses={TRACK: analysis})
    assert cleared.sample.deck == "1"
    assert cleared.profile is None
    assert _feeds(builder, 3, loaded=False, analyses={}) == ()


def test_missing_analysis_profile_none_and_feed_runs() -> None:
    """[if] the analysis is missing or unusable [then] the profile is None and batches keep coming, [else stop]."""
    builder = DeckFeedBuilder()
    (no_analysis,) = _feeds(builder, 1, loaded=True, analyses={})
    assert no_analysis.profile is None
    broken = analysis_from_anlz(TRACK, {"beatgrid": None}, DURATION_MS, profile_from_payloads)
    assert broken.profile is None
    (no_profile,) = _feeds(builder, 2, loaded=True, analyses={TRACK: broken})
    assert no_profile.profile is None
    assert analysis_from_anlz(TRACK, anlz_payload(TRACK), DURATION_MS).profile is None
    assert len(_feeds(builder, 3, loaded=True, analyses={TRACK: broken})) == 1
