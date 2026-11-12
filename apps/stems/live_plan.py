"""Live-stems lookahead planning and CPU deadline policy ((private path)).

Lookahead calculator (LATENCY-04 deck windows):
  No capability record, HTTP, CLI, or filesystem access. Locks four- and eight-bar
  lookahead milliseconds and the exclusive lookahead PCM byte estimate.

  [if] a 2-deck plan runs at 120 BPM [then ⛔️]
  [if] a 4-deck plan runs at 120 BPM [then ⛔️]
  [if] a caller requests an unsupported deck count [then ⛔️]
  [if] BPM is zero or non-finite [then ⛔️]

Deadline evaluator:
  [if] inference finishes strictly before lookahead_ms [then] enable the deadline check.
  [if] inference finishes exactly at lookahead_ms [then] refuse the plan.
  [if] inference exceeds lookahead_ms [then] refuse the plan.
  [if] inference duration is negative or non-finite [then] raise a validation error.

The deadline path evaluates only timing; it does not assess other capability gates.

Working-set RSS evaluator (RB-PERFORMANCE-12829):
  Compares a supplied live-generation working-set RSS in MiB (model, activations,
  lookahead PCM; not decoded playback PCM) with the inclusive quality ceiling.
  Excess returns False; disabled quality and invalid inputs raise. It collects no
  measurements and makes no claim that a live generator has been benchmarked.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Final

from apps.stems.live_capability import (
    FOUR_DECK_LOOKAHEAD_BARS,
    HIGH_QUALITY_WORKING_SET_CEILING_MIB,
    LOW_QUALITY_WORKING_SET_CEILING_MIB,
    MAX_PLAN_BPM,
    MIN_PLAN_BPM,
    SUPPORTED_DECK_COUNTS,
    TWO_DECK_LOOKAHEAD_BARS,
    LiveStemsCapabilityError,
    LiveStemsPlan,
    LiveStemsQuality,
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


@dataclass(frozen=True)
class LiveStemsDeadlineEvaluation:
    """Deadline decision retaining the measured duration and planned window."""

    enabled: bool
    reason: str
    inference_ms: float
    lookahead_ms: int


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


def evaluate_live_stems_deadline(plan: LiveStemsPlan, *, inference_ms: float) -> LiveStemsDeadlineEvaluation:
    """Require completion strictly before the plan's BPM-derived deadline."""
    if not math.isfinite(inference_ms) or inference_ms < 0:
        raise LiveStemsCapabilityError(f"inference duration must be finite and nonnegative, got {inference_ms} ms")

    enabled = inference_ms < plan.lookahead_ms
    if enabled:
        reason = (
            f"inference finished in {inference_ms} ms, strictly before the {plan.lookahead_ms} ms lookahead deadline"
        )
    else:
        reason = (
            f"live stems refused for this plan: inference took {inference_ms} ms; "
            f"must finish strictly before the {plan.lookahead_ms} ms lookahead deadline"
        )
    return LiveStemsDeadlineEvaluation(
        enabled=enabled,
        reason=reason,
        inference_ms=inference_ms,
        lookahead_ms=plan.lookahead_ms,
    )


def evaluate_live_generation_rss(quality: LiveStemsQuality, *, working_set_rss_mib: float) -> bool:
    """Compare supplied RSS with its quality ceiling; callers must honor False."""
    if not isinstance(quality, LiveStemsQuality):
        raise LiveStemsCapabilityError(f"invalid live-generation quality for working-set RSS in MiB: {quality!r}")
    if quality is LiveStemsQuality.LOW:
        ceiling_mib = LOW_QUALITY_WORKING_SET_CEILING_MIB
    elif quality is LiveStemsQuality.HIGH:
        ceiling_mib = HIGH_QUALITY_WORKING_SET_CEILING_MIB
    else:
        raise LiveStemsCapabilityError("disabled quality has no live-generation working-set RSS ceiling in MiB")

    if (
        isinstance(working_set_rss_mib, bool)
        or not isinstance(working_set_rss_mib, (int, float))
        or working_set_rss_mib < 0
        or (isinstance(working_set_rss_mib, float) and not math.isfinite(working_set_rss_mib))
    ):
        raise LiveStemsCapabilityError(
            f"live-generation working-set RSS must be a finite nonnegative number in MiB, got {working_set_rss_mib!r}"
        )
    return working_set_rss_mib <= ceiling_mib
