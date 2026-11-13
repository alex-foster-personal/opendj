"""Frame-level lighting clock selection, independent of feed or HTTP transport."""

from __future__ import annotations

from collections.abc import Iterable

from apps.lighting.beat_clock import DeckSample
from apps.lighting.contract import ClockSnapshot


def select_frame_clock(snapshot: ClockSnapshot, present_decks: Iterable[str]) -> str | None:
    """Emit only for a loaded, resolved source actually present in this feed frame."""
    deck = snapshot.resolve().effective_deck
    return deck if deck is not None and deck in present_decks else None


def can_publish_sections(sample: DeckSample, previous: float, *, selected: bool, handover: bool) -> bool:
    """Only the continuously selected, advancing source publishes phrase boundaries."""
    return selected and not handover and sample.playing and sample.position_ms >= previous
