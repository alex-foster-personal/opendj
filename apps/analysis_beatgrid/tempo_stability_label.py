"""The library BPM Stability column's words and tone (issue #7879).

The column shows what the tempo-stability verdict (`tempo_stability.py`)
decided; this module only turns that verdict into the #7879 strings, so the
frontend computes nothing (the #7879 veto). Thresholds are #7879's, mimicked:
amber under 5 percent, red 5 to 15, deep red 15 and over.

Requirements (mini-PRD):
  - Text is one of `steady`, `drift <1%`, `drift ±N%` (whole N), `free intro`,
    `N x change` (a multiplication sign), `unstable`.
    [if] any drift text carries a decimal or reads "±0%" [then] broken
  - The `changes` text uses the detector-confirmed `change_count`.
    [if] a rekordbox dynamic grid with dozens of per-beat steps reads
      "85 x change" [then] broken (it reads `unstable`)
  - [if] a track with 3 changes, largest 18 percent, is not deep red [then] broken
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from apps.analysis_beatgrid.tempo_stability import Kind, Verdict

Tone = Literal["none", "amber", "red", "deep_red"]

#: #7879: amber under 5 percent, red 5 to 15, deep red 15 and over.
RED_FROM_PCT = 5.0
DEEP_RED_FROM_PCT = 15.0

_VERDICT_SENTENCE: dict[str, str] = {
    "steady": "Tempo holds steady for the whole track",
    "drift": "Tempo drifts but never settles into a new tempo",
    "free_intro": "Free-time intro, then a steady tempo",
    "changes": "Tempo changes",
    "unstable": "Beatgrid tempo is unstable (many small per-beat steps)",
}


@dataclass(frozen=True)
class TempoLabel:
    text: str
    tone: Tone
    #: Hover title: the verdict in words, then the beatgrid verdict sentence.
    title: str


def _whole_pct(pct: float) -> int:
    """Half-up whole percent (Python's round() is banker's rounding)."""
    return math.floor(pct + 0.5)


def tempo_tone(verdict: Verdict, largest_change_pct: float) -> Tone:
    if verdict == "steady":
        return "none"
    if largest_change_pct >= DEEP_RED_FROM_PCT:
        return "deep_red"
    if largest_change_pct >= RED_FROM_PCT:
        return "red"
    return "amber"


def tempo_label(
    verdict: Verdict,
    largest_change_pct: float,
    change_count: int,
    segment_kinds: Sequence[Kind],
    grid_message: str,
) -> TempoLabel:
    """The #7879 column text for one stored verdict."""
    key: str = verdict
    if verdict == "steady":
        text = "steady"
    elif verdict == "free_intro":
        text = "free intro"
    elif verdict == "changes":
        text = f"{change_count}\u00d7 change"
    elif verdict == "drift":
        if "unstable" in segment_kinds:
            # A grid that jitters (a rekordbox dynamic grid's per-beat steps, a
            # folded half or double time stretch) says nothing about a drift size.
            key, text = "unstable", "unstable"
        elif largest_change_pct < 1.0:
            text = "drift <1%"
        else:
            text = f"drift ±{_whole_pct(largest_change_pct)}%"
    else:
        raise ValueError(f"unhandled tempo verdict: {verdict!r}")
    sentence = _VERDICT_SENTENCE[key]
    if verdict != "steady":
        plural = "s" if change_count != 1 else ""
        size = "under 1%" if largest_change_pct < 1.0 else f"{_whole_pct(largest_change_pct)}%"
        sentence += f" (largest move {size} from the main tempo, {change_count} confirmed change{plural})"
    return TempoLabel(text=text, tone=tempo_tone(verdict, largest_change_pct), title=f"{sentence}. {grid_message}")


__all__ = ["DEEP_RED_FROM_PCT", "RED_FROM_PCT", "TempoLabel", "Tone", "tempo_label", "tempo_tone"]
