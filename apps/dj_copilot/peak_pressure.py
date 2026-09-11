"""Peak energy pressure coach (AI-05, issue #342).

Educational metadata-only score: how hard the DJ has been leaning on
peak-suitable tracks from library energy and optional peak tags. No
crowd vision input; metadata is not crowd response.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .rank_stage1 import ScoredCandidate

WINDOW = 8
STEP_UP = 0.18
STEP_DOWN = 0.22
RELEASE_AT = 0.70
BUILD_BELOW = 0.40
MIN_SCORED = 2

RELEASE_BUMP = 0.04
RELEASE_PENALTY = 0.03
KEEP_BUMP = 0.02

_PEAK_TAGS = frozenset(
    {"peak", "peak-time", "peak_time", "peak time", "good for peak"}
)

_ADVISORY = "Educational coach from track metadata, not the room."
_LIMITATION = "Metadata energy is not crowd response."

_CUE_LABELS: dict[str, str] = {
    "keep_building": "keep building",
    "hold": "hold",
    "release": "time to release a little",
    "unknown": "",
}

PressureCue = Literal["keep_building", "hold", "release", "unknown"]


@dataclass(frozen=True, slots=True)
class PeakPlay:
    stable_id: str
    energy: int | None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PeakPressure:
    score: float
    cue: PressureCue
    scored_tracks: int
    skipped_unknown: int
    cue_label: str
    advisory: str
    limitation: str


def is_peak_suitable(play: PeakPlay) -> bool | None:
    """Return True/False when scorable; None when energy+tags are unknown."""
    for tag in play.tags:
        if tag.casefold().strip() in _PEAK_TAGS:
            return True
    if play.energy is None or not (1 <= play.energy <= 10):
        return None
    return play.energy >= 7


def compute_peak_pressure(plays: Sequence[PeakPlay]) -> PeakPressure:
    """Walk the timeline and return pressure score plus advisory cue."""
    scored: list[bool] = []
    skipped_unknown = 0
    for play in plays:
        suitable = is_peak_suitable(play)
        if suitable is None:
            skipped_unknown += 1
            continue
        scored.append(suitable)

    window = scored[-WINDOW:] if len(scored) > WINDOW else scored
    pressure = 0.0
    for suitable in window:
        pressure = (
            min(1.0, pressure + STEP_UP)
            if suitable
            else max(0.0, pressure - STEP_DOWN)
        )

    score = round(pressure, 3)
    scored_tracks = len(window)
    if scored_tracks < MIN_SCORED:
        cue: PressureCue = "unknown"
    elif score >= RELEASE_AT:
        cue = "release"
    elif score < BUILD_BELOW:
        cue = "keep_building"
    else:
        cue = "hold"

    return PeakPressure(
        score=score,
        cue=cue,
        scored_tracks=scored_tracks,
        skipped_unknown=skipped_unknown,
        cue_label=_CUE_LABELS[cue],
        advisory=_ADVISORY,
        limitation=_LIMITATION,
    )


def apply_pressure_prior(
    stage2: list[ScoredCandidate], pressure: PeakPressure
) -> list[ScoredCandidate]:
    """Soft score nudge after pairings; never drops candidates."""
    if pressure.cue in ("hold", "unknown"):
        return stage2

    out: list[ScoredCandidate] = []
    for sc in stage2:
        energy = sc.candidate.energy if sc.candidate else None
        rationale = dict(sc.rationale)
        score = sc.score

        if pressure.cue == "release":
            if energy is not None and energy <= 6:
                score += RELEASE_BUMP
                rationale["pressure_release"] = 1.0
            elif energy is not None and energy >= 8:
                score -= RELEASE_PENALTY
        elif (
            pressure.cue == "keep_building"
            and energy is not None
            and energy >= 7
        ):
            score += KEEP_BUMP

        out.append(
            ScoredCandidate(
                stable_id=sc.stable_id,
                score=score,
                rationale=rationale,
                candidate=sc.candidate,
            )
        )

    out.sort(key=lambda c: (-c.score, c.stable_id))
    return out
