"""Synthesized beat grids exercise the real runtime clock event path.

[if] the selected clock changes [then] only its current events publish, [else stop].
"""

import pytest

from apps.lighting.beat_clock import DeckSample
from apps.lighting.contract import (
    ClockSnapshot,
    ClockSourceError,
    normalize_clock_deck,
    parse_clock_source,
)
from apps.lighting.runtime import DeckFeed, FeedFrame, LightingRuntime
from apps.lighting.sections import LightSection, SectionSpan, TrackMood

pytestmark = pytest.mark.requirement("LIGHT-12848")

GRID = tuple({"n": i % 4 + 1, "time_ms": i * 500.0, "bpm": 120.0} for i in range(16))
SECTIONS = (
    SectionSpan(0, 1000, LightSection.INTRO, TrackMood.MID, "intro"),
    SectionSpan(1000, 2000, LightSection.DROP, TrackMood.HIGH, "drop"),
    SectionSpan(2000, 8000, LightSection.OUTRO, TrackMood.MID, "outro"),
)


def _frame(position: float, decks: tuple[str, ...] = ("1", "2")) -> FeedFrame:
    host_ns = 1_000_000_000 + round(position * 1_000_000)
    return FeedFrame(
        host_time_ns=host_ns,
        decks=tuple(DeckFeed(DeckSample(deck, position, 1.0, True, host_ns), GRID, SECTIONS) for deck in decks),
        weights={deck: 0.5 for deck in decks},
    )


def _clock_events(runtime: LightingRuntime, position: float) -> list[dict[str, object]]:
    return [event for event in runtime.ingest(_frame(position)) if event["type"] != "deck_weights"]


def test_selection_changes_live_without_replaying_phrase_history() -> None:
    snapshot = ClockSnapshot("master", "2", frozenset({"1", "2"}))
    runtime = LightingRuntime(clock_provider=lambda: snapshot)
    assert {event["deck"] for event in _clock_events(runtime, 0)} == {"2"}
    snapshot = ClockSnapshot("deck-1", "2", snapshot.loaded_decks)
    switched = _clock_events(runtime, 1500)
    assert {event["deck"] for event in switched} == {"1"}
    assert all(event["type"] == "beat" for event in switched)
    assert _clock_events(runtime, 1500) == []
    snapshot = ClockSnapshot("master", "2", snapshot.loaded_decks)
    assert {event["deck"] for event in _clock_events(runtime, 2000)} == {"2"}
    assert not any(event["type"] == "section_change" for event in _clock_events(runtime, 2000))
    assert set(runtime.status()["weights"]) == {"1", "2"}
    assert {deck.deck for deck in runtime.show_decks(_frame(2000))} == {"1", "2"}


def test_phrases_follow_selected_source_and_seeks_reset_beats() -> None:
    runtime = LightingRuntime(clock_provider=lambda: ClockSnapshot("master", "2", frozenset({"1", "2"})))
    runtime.ingest(_frame(500))
    events = _clock_events(runtime, 1000)
    assert {event["deck"] for event in events} == {"2"}
    assert sum(event["type"] == "section_change" for event in events) == 1
    assert _clock_events(runtime, 1000) == []
    assert any(event["type"] == "beat" for event in _clock_events(runtime, 500))


def test_missing_context_and_unload_suppress_clock_and_clear_cursors() -> None:
    runtime = LightingRuntime()
    assert _clock_events(runtime, 0) == []
    snapshot = ClockSnapshot("deck-1", "2", frozenset({"1", "2"}))
    runtime.clock_provider = lambda: snapshot
    assert {event["deck"] for event in _clock_events(runtime, 0)} == {"1"}
    snapshot = ClockSnapshot("deck-1", "2", frozenset({"2"}))
    assert _clock_events(runtime, 500) == []
    snapshot = ClockSnapshot("deck-1", "2", frozenset({"1", "2"}))
    assert {event["deck"] for event in _clock_events(runtime, 0)} == {"1"}
    runtime.ingest(_frame(500, ("2",)))
    assert {deck["deck"] for deck in runtime.status()["decks"]} == {"2"}
    assert {event["deck"] for event in _clock_events(runtime, 0)} == {"1"}


def test_a_b_a_handover_and_same_effective_deck_do_not_repeat_beats() -> None:
    snapshot = ClockSnapshot("master", "2", frozenset({"1", "2"}))
    runtime = LightingRuntime(clock_provider=lambda: snapshot)
    assert len(_clock_events(runtime, 500)) == 1
    snapshot = ClockSnapshot("deck-1", "2", snapshot.loaded_decks)
    assert len(_clock_events(runtime, 500)) == 1
    snapshot = ClockSnapshot("master", "2", snapshot.loaded_decks)
    assert _clock_events(runtime, 500) == []
    snapshot = ClockSnapshot("deck-2", "2", snapshot.loaded_decks)
    assert _clock_events(runtime, 500) == []


def test_grid_replacement_resets_beat_cursor() -> None:
    runtime = LightingRuntime(clock_provider=lambda: ClockSnapshot("deck-1", "2", frozenset({"1", "2"})))
    assert len(_clock_events(runtime, 500)) == 1
    frame = _frame(500)
    replaced = FeedFrame(
        frame.host_time_ns,
        tuple(DeckFeed(feed.sample, list(GRID), SECTIONS) for feed in frame.decks),
        frame.weights,
    )
    assert len([event for event in runtime.ingest(replaced) if event["type"] == "beat"]) == 1


@pytest.mark.parametrize("source", ["deck-0", "deck-9", "deck-01", "deck-1 ", "MASTER", ""])
def test_bad_selectors_fail_without_a_default(source: str) -> None:
    with pytest.raises(ClockSourceError) as error:
        parse_clock_source(source)
    assert error.value.status_code == 400
    assert error.value.code == "lighting_clock_bad_deck"


@pytest.mark.parametrize("value", [True, False, 0, 5, 1.0, "deck-1", "01", None])
def test_normalizer_rejects_noncanonical_mirror_deck_identifiers(value: object) -> None:
    assert normalize_clock_deck(value) is None


@pytest.mark.parametrize("value", [1, "1", 4, "4"])
def test_normalizer_accepts_supported_mirror_deck_identifiers(value: object) -> None:
    assert normalize_clock_deck(value) == str(value)
