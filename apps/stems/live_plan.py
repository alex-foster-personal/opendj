"""Pure live-stems lookahead plan calculator (LATENCY-04 deck windows).

No capability record, HTTP, CLI, or filesystem access. Locks four- and eight-bar
lookahead milliseconds and the exclusive lookahead PCM byte estimate per
(private path).

Requirements:
  ✔︎ Two decks use a four-bar lookahead window.
  ✔︎ Four decks use an eight-bar lookahead window.
  ✔︎ Unsupported deck counts and invalid BPM values are refused.

Acceptance tests:
  [if] a 2-deck plan runs at 120 BPM [then ⛔️]
  [if] a 4-deck plan runs at 120 BPM [then ⛔️]
  [if] a caller requests an unsupported deck count [then ⛔️]
  [if] BPM is zero or non-finite [then ⛔️]
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Final

from apps.stems.live_capability import (
    FOUR_DECK_LOOKAHEAD_BARS,
    MAX_PLAN_BPM,
    MIN_PLAN_BPM,
    SUPPORTED_DECK_COUNTS,
    TWO_DECK_LOOKAHEAD_BARS,
)

LIVE_STEMS_SAMPLE_RATE_HZ: Final[int] = 48_000
LIVE_STEMS_CHANNELS: Final[int] = 2
LIVE_STEMS_BYTES_PER_SAMPLE: Final[int] = 4
LIVE_STEMS_PART_COUNT: Final[int] = 4


class LiveStemsPlanError(ValueError):
    """Invalid deck plan inputs for the live-stems lookahead envelope."""


@dataclass(frozen=True)
class LiveStemsLookaheadPlan:
    """Exclusive lookahead PCM budget for a deck count and tempo."""

    deck_count: int
    bpm: float
    lookahead_bars: int
    lookahead_ms: int
    lookahead_pcm_bytes: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _lookahead_bars(deck_count: int) -> int:
    if deck_count == 2:
        return TWO_DECK_LOOKAHEAD_BARS
    return FOUR_DECK_LOOKAHEAD_BARS


def _validate_bpm(bpm: float) -> None:
    if not math.isfinite(bpm):
        raise LiveStemsPlanError(f"BPM must be finite, got {bpm}")
    if not MIN_PLAN_BPM <= bpm <= MAX_PLAN_BPM:
        raise LiveStemsPlanError(f"BPM must be between {MIN_PLAN_BPM:g} and {MAX_PLAN_BPM:g}, got {bpm}")


def _lookahead_pcm_bytes(*, lookahead_ms: int, deck_count: int) -> int:
    return (
        lookahead_ms
        * LIVE_STEMS_SAMPLE_RATE_HZ
        * LIVE_STEMS_CHANNELS
        * LIVE_STEMS_BYTES_PER_SAMPLE
        * LIVE_STEMS_PART_COUNT
        * deck_count
    ) // 1_000


def plan_live_stems_lookahead(*, deck_count: int, bpm: float) -> LiveStemsLookaheadPlan:
    """Return locked lookahead ms and exclusive PCM bytes for deck_count and bpm."""
    if deck_count not in SUPPORTED_DECK_COUNTS:
        raise LiveStemsPlanError(f"live stems supports deck_count {sorted(SUPPORTED_DECK_COUNTS)}, got {deck_count}")
    _validate_bpm(bpm)
    lookahead_bars = _lookahead_bars(deck_count)
    lookahead_ms = round(lookahead_bars * 4 * 60_000 / bpm)
    lookahead_pcm_bytes = _lookahead_pcm_bytes(lookahead_ms=lookahead_ms, deck_count=deck_count)
    return LiveStemsLookaheadPlan(
        deck_count=deck_count,
        bpm=bpm,
        lookahead_bars=lookahead_bars,
        lookahead_ms=lookahead_ms,
        lookahead_pcm_bytes=lookahead_pcm_bytes,
    )
