"""Regression contract for the four-deck performance waveform stack.

These assertions inspect the shipped Svelte components because the frontend's
unit runner intentionally transpiles each module in isolation, while this
contract covers the composed markup that the real browser receives.

Regression one-liners:
  - if the performance waveform stack stops mounting exactly decks 1-4 then broken
  - if a waveform row loses its per-deck identity then broken
  - if an empty deck exposes a seekable waveform control then broken
  - if waveform seeking loses its accessible slider contract then broken
  - if a loaded deck loses its artwork, readable truncated title, or hover scrub then broken
  - if an empty or artwork-unavailable deck looks like a silent missing thumbnail then broken
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.requirement("CAT-05"), pytest.mark.rb_parity]

REPO_ROOT = Path(__file__).resolve().parents[2]
WAVEFORM_STACK = REPO_ROOT / "apps/webui/frontend/src/lib/components/rb/WaveformStack.svelte"
WAVE_ROW = REPO_ROOT / "apps/webui/frontend/src/lib/components/rb/wave/WaveRow.svelte"
WAVE_GUTTER = REPO_ROOT / "apps/webui/frontend/src/lib/components/rb/wave/WaveGutter.svelte"
WAVE_TRACK_SUMMARY = (
    REPO_ROOT / "apps/webui/frontend/src/lib/components/rb/wave/WaveTrackSummary.svelte"
)

# The deck number span may carry extra attributes (the explainer audit added a
# hover `title`), but it must keep its class and still render the deck id.
DECK_NUM_SPAN = re.compile(r'<span class="deck-num"[^>]*>\{deckId\}</span>')


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_wavestack_mounts_exactly_the_four_supported_decks() -> None:
    source = _source(WAVEFORM_STACK)

    assert "const deckIds: DeckId[] = [1, 2, 3, 4];" in source
    assert "{#each deckIds as deckId (deckId)}" in source
    assert "<WaveRow {deckId} />" in source


def test_every_wavestack_row_carries_its_deck_identity() -> None:
    """[if] a wavestack row drops its deck id span [then] fail, [else stop]."""
    source = _source(WAVE_ROW)

    gutter = _source(WAVE_GUTTER)

    assert 'class="rb-waverow"' in source
    assert "data-deck={deckId}" in source
    assert '<WaveGutter {deck} {deckId} {barsLabel} />' in source
    assert DECK_NUM_SPAN.search(gutter), (
        f"{WAVE_GUTTER.name} must render the deck id inside a .deck-num span; "
        f"no match in:\n{gutter}"
    )


def test_empty_or_busy_deck_waveform_is_not_seekable() -> None:
    source = _source(WAVE_ROW)

    assert "deck.stable_id === null || (commandPending && !seeking)" in source
    assert "aria-disabled={deck.stable_id === null || (commandPending && !seeking)}" in source
    assert "if (\n\t\t\t!event.isPrimary ||" in source
    assert "deck.stable_id === null ||" in source
    assert "deck.duration_ms === null ||" in source
    assert "commandPending" in source


def test_waveform_seek_canvas_remains_an_accessible_bounded_slider() -> None:
    source = _source(WAVE_ROW)

    assert 'role="slider"' in source
    assert 'aria-label="deck {deckId} waveform seek"' in source
    assert "aria-valuemin={0}" in source
    assert "aria-valuemax={deck.duration_ms ?? 0}" in source
    assert "aria-valuenow={Math.round(deck.position_ms)}" in source


def test_waveform_gutter_has_track_artwork_and_hover_scrubbable_title() -> None:
    source = _source(WAVE_TRACK_SUMMARY)

    # PARITY-14: the deck asks for the online lookup too, so no local-miss skip.
    assert "deckArtworkUrl(deck.stable_id, 's')" in source
    assert 'class="wave-art"' in source
    assert 'class="wave-track-name"' in source
    assert "trackNameScrubPx" in source
    assert "onpointerenter={startTrackNameScrub}" in source
    assert "onpointerleave={stopTrackNameScrub}" in source


def test_waveform_gutter_labels_empty_and_unavailable_artwork_states() -> None:
    source = _source(WAVE_TRACK_SUMMARY)

    assert "No track loaded" in source
    assert "No artwork found" in source
    assert "artworkFailed" in source
    assert "loaded deck ${deck.stable_id} is missing a title" in source
