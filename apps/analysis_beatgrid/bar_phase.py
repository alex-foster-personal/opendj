"""Bar phase: turn beat and downbeat times into 1-to-4 bar positions, or refuse.

WHY THIS IS NOT `beat_this.utils.infer_beat_numbers`. That helper counts upward
between consecutive downbeats and never wraps, so a bar whose downbeat the
model missed comes back numbered 1,2,3,4,5,6,7,8. It is right for a library
that must support arbitrary meters and cannot assume the bar length. This
project can: rekordbox's PQTZ grid stores `n` as a position in a four-beat bar
and the `/anlz` payload hands the deck exactly that, so an `n` of 7 is not a
longer bar, it is a number no consumer can place. Measured on the committed
round-1 artifact `ops/beatbench/round-1/raw-beatgrid_lane_t050.json`, 90 tracks:
24 carried a number above 4 and one reached 20 (Codex P1 on PR #1514).

SO THE NUMBER IS A PHASE, AND A PHASE NEEDS AN ANCHOR. Every beat is numbered
by its distance from the nearest PRECEDING downbeat, modulo the bar length.
That keeps the arithmetic total -- there is always an answer in 1..4 -- and it
means a missed downbeat costs a wrong bar-1 position for one bar rather than
poisoning every number after it.

WRAPPING ALONE WOULD HIDE THE MISS, which is the failure mode to avoid: a
result where 1,2,3,4,1,2,3,4 is indistinguishable from two real bars is worse
than the n=8 it replaced, because the n=8 at least announced itself. So the
anomalies survive into the result as counts a reader can act on:
`n_bars_over_length` (a downbeat was missed), `n_bars_under_length` (a spurious
downbeat, or a genuine meter this lane does not model) and `max_bar_beats`.

AND WHEN NOTHING ANCHORS THE RUN, THE PHASE IS UNESTABLISHED. With no downbeat
anywhere there is no bar-1, so numbering every beat from an arbitrary start
would be an invention. `bar_phase_unestablished` is returned with the reason
named, `beat_numbers` is empty, and the caller is expected to publish the grid
without a bar phase rather than with a guessed one. It is the same shape as
`flags.PulseFlag.no_trackable_pulse` and shares its `no_downbeat_anchor`
reason string, because it is the same condition seen from the bar side.

A PICKUP IS ANCHORED, NOT UNESTABLISHED. Beats before the first downbeat are
numbered by counting BACKWARDS from it, which is deterministic and uses a real
anchor, just a following one. Measured on the same artifact: 45 of the 86
tracks that have any downbeat at all begin with beats before the first, so
treating a pickup as unestablished would flag half the corpus for an ordinary
musical feature. The count is reported as `n_backprojected_beats` so it is
visible rather than assumed away.
"""

from __future__ import annotations

import bisect
import itertools
from collections.abc import Sequence
from dataclasses import dataclass

from apps.analysis_beatgrid.flags import REASON_NO_DOWNBEAT_ANCHOR

# ----- Named constants ----------------------------------------------------

# rekordbox PQTZ stores a beat number in 1..4 and the deck reads it as a bar
# position. This lane emits the same contract rather than an arbitrary meter.
BAR_BEATS = 4

# A downbeat must BE one of the beats. The producer snaps them before emitting,
# so anything further than a float round-trip apart is an upstream defect, not
# a value to be quietly snapped here.
DOWNBEAT_MATCH_TOLERANCE_S = 1e-6

REASON_NO_BEATS = "no_beats"

# An off-phase downbeat closer than this to a chosen-phase one is a detector
# double (round 2: 249 gap-1 + 325 gap-2), not a vote against the phase. Gap 3
# is a short bar, not a double.
DOUBLE_MIN_GAP_BEATS = 3

# Fail a track whose thinned-downbeat agreement with the chosen phase is below
# this. Locked from the round-3 90-track measure (ops/beatbench/round-3/).
BAR_PHASE_AGREEMENT_FLOOR = 0.50

REASON_BAR_PHASE_BELOW_FLOOR = "bar_phase_below_floor"

__all__ = [
    "BAR_BEATS",
    "BAR_PHASE_AGREEMENT_FLOOR",
    "DOUBLE_MIN_GAP_BEATS",
    "DOWNBEAT_MATCH_TOLERANCE_S",
    "REASON_BAR_PHASE_BELOW_FLOOR",
    "REASON_NO_BEATS",
    "REASON_NO_DOWNBEAT_ANCHOR",
    "BarPhase",
    "PhaseVote",
    "assign_bar_phase",
    "lock_bar_phase",
    "vote_bar_phase",
]


@dataclass(frozen=True)
class BarPhase:
    """A bar position for every beat, plus what had to be assumed to get there."""

    beat_numbers: list[int]
    bar_phase_unestablished: bool
    reason: str | None
    n_beats: int
    n_downbeats: int
    n_backprojected_beats: int
    n_bars_over_length: int
    n_bars_under_length: int
    max_bar_beats: int
    chosen_phase: int | None = None
    phase_agreement: float | None = None
    n_phase_disagreements: int = 0
    n_downbeats_thinned: int = 0


# ----- Helpers ------------------------------------------------------------


def _downbeat_indices(
    beats: Sequence[float], downbeats: Sequence[float], tolerance_s: float
) -> list[int]:
    """Index of each downbeat within `beats`, or raise naming the offender."""
    indices: list[int] = []
    for downbeat in downbeats:
        position = bisect.bisect_left(beats, downbeat - tolerance_s)
        if position >= len(beats) or abs(beats[position] - downbeat) > tolerance_s:
            raise ValueError(
                f"downbeat at {downbeat}s is not a beat (nearest beat is more than "
                f"{tolerance_s}s away); snap downbeats onto beats before numbering"
            )
        indices.append(position)
    return sorted(set(indices))


def _bar_lengths(anchors: list[int], n_beats: int) -> tuple[list[int], int]:
    """`(closed bar lengths, length of the final open bar)` in beats."""
    closed = [b - a for a, b in itertools.pairwise(anchors)]
    return closed, n_beats - anchors[-1]


def _ascending_floats(beat_times: Sequence[float]) -> list[float]:
    """`beat_times` as floats, or raise: a grid out of order is malformed."""
    beats = [float(t) for t in beat_times]
    if any(later <= earlier for earlier, later in itertools.pairwise(beats)):
        raise ValueError("beat_times must be strictly ascending")
    return beats


def _phase_numbers(n_beats: int, anchors: list[int], bar_beats: int) -> list[int]:
    """Every beat's distance from its nearest PRECEDING anchor, wrapped to 1..n.

    Beats before the first downbeat count backwards from it; Python's modulo is
    non-negative, so one expression covers both directions.
    """
    numbers: list[int] = []
    anchor_slot = 0
    for index in range(n_beats):
        while anchor_slot + 1 < len(anchors) and anchors[anchor_slot + 1] <= index:
            anchor_slot += 1
        numbers.append((index - anchors[anchor_slot]) % bar_beats + 1)
    return numbers


def _bar_anomalies(closed: list[int], open_bar: int, bar_beats: int) -> tuple[int, int, int]:
    """`(over-length bars, under-length bars, longest bar)` in beats.

    An open final bar can be too LONG (a missed downbeat) but never too short:
    the excerpt simply ended, which is not a grid defect.
    """
    return (
        sum(1 for n in closed if n > bar_beats) + (1 if open_bar > bar_beats else 0),
        sum(1 for n in closed if n < bar_beats),
        max([*closed, open_bar]),
    )


# ----- The policy ---------------------------------------------------------


def assign_bar_phase(
    beat_times: Sequence[float],
    downbeat_times: Sequence[float],
    *,
    bar_beats: int = BAR_BEATS,
    tolerance_s: float = DOWNBEAT_MATCH_TOLERANCE_S,
) -> BarPhase:
    """Number every beat 1..`bar_beats` from its nearest preceding downbeat.

    Raises when `beat_times` is not strictly ascending or when a downbeat is
    not one of the beats: both mean the caller's grid is malformed, and a
    repaired-in-silence grid is indistinguishable from a correct one.
    """
    if bar_beats < 1:
        raise ValueError(f"bar_beats must be at least 1, got {bar_beats}")

    beats = _ascending_floats(beat_times)
    downbeats = sorted(float(t) for t in downbeat_times)
    if not beats:
        return BarPhase([], True, REASON_NO_BEATS, 0, len(downbeats), 0, 0, 0, 0)

    anchors = _downbeat_indices(beats, downbeats, tolerance_s)
    if not anchors:
        return BarPhase([], True, REASON_NO_DOWNBEAT_ANCHOR, len(beats), 0, 0, 0, 0, 0)

    closed, open_bar = _bar_lengths(anchors, len(beats))
    over, under, longest = _bar_anomalies(closed, open_bar, bar_beats)
    return BarPhase(
        beat_numbers=_phase_numbers(len(beats), anchors, bar_beats),
        bar_phase_unestablished=False,
        reason=None,
        n_beats=len(beats),
        n_downbeats=len(anchors),
        n_backprojected_beats=anchors[0],
        n_bars_over_length=over,
        n_bars_under_length=under,
        max_bar_beats=longest,
    )


@dataclass(frozen=True)
class PhaseVote:
    """The bar phase every downbeat voted for, and how well the bars agree."""

    chosen: int
    agreement: float
    n_disagreements: int
    n_doubles: int


def vote_bar_phase(anchors: Sequence[int], bar_beats: int = BAR_BEATS) -> PhaseVote:
    """Majority bar phase over EVERY downbeat index; doubles are not votes against.

    WHY NOT THIN FIRST. The vote used to drop the later of any two downbeats
    closer than `DOUBLE_MIN_GAP_BEATS`, walking forward, then vote on what
    was left. That lets the FIRST downbeat of a half-bar double pattern pick
    the phase: a model that marks bar-1 on 21 bars and beat 3 on 14 of them
    keeps whichever came first, then votes consistently for it. Measured on
    round 6 (200 fixed-tempo fixtures, line_round_offset): voting on every
    downbeat puts 6 more tracks on rekordbox's bar-1, and the one track it
    loses (2 fixtures) now fails closed rather than serving a wrong bar.

    AGREEMENT. Once the phase is chosen, an off-phase downbeat closer than
    `DOUBLE_MIN_GAP_BEATS` to a chosen-phase one is a double and is left out;
    every other off-phase downbeat disagrees. Agreement is chosen-phase
    downbeats over chosen-phase plus disagreeing ones.

    AN EVERY-BEAT STREAM MUST NOT PASS. With doubles left out, a model that
    fires on every beat would score 1.0. When doubles outnumber the
    chosen-phase downbeats (more than two downbeats per marked bar), the
    model is not marking bars, and agreement is the share of ALL downbeats
    on the chosen phase instead: 1 / `bar_beats` for that stream.

    Ties go to the smallest phase index, as before.
    """
    if not anchors:
        raise ValueError("vote_bar_phase needs at least one downbeat index")
    votes = [0] * bar_beats
    for anchor in anchors:
        votes[anchor % bar_beats] += 1
    chosen = votes.index(max(votes))
    on_phase = sorted(a for a in anchors if a % bar_beats == chosen)
    n_doubles = 0
    for anchor in anchors:
        if anchor % bar_beats == chosen:
            continue
        slot = bisect.bisect_left(on_phase, anchor)
        near = [on_phase[j] for j in (slot - 1, slot) if 0 <= j < len(on_phase)]
        if any(abs(anchor - other) < DOUBLE_MIN_GAP_BEATS for other in near):
            n_doubles += 1
    n_disagree = len(anchors) - len(on_phase) - n_doubles
    # More doubles than on-phase marks (an every-beat stream) is scored against all marks.
    denominator = len(anchors) if n_doubles > len(on_phase) else len(on_phase) + n_disagree
    agreement = len(on_phase) / denominator
    return PhaseVote(
        chosen=chosen,
        agreement=agreement,
        n_disagreements=n_disagree,
        n_doubles=n_doubles,
    )


def _lock_numbers(n_beats: int, chosen: int, bar_beats: int) -> list[int]:
    """Every beat numbered from the chosen phase anchor."""
    return [(index - chosen) % bar_beats + 1 for index in range(n_beats)]


def lock_bar_phase(
    beat_times: Sequence[float],
    downbeat_times: Sequence[float],
    *,
    bar_beats: int = BAR_BEATS,
    tolerance_s: float = DOWNBEAT_MATCH_TOLERANCE_S,
    floor: float = BAR_PHASE_AGREEMENT_FLOOR,
) -> BarPhase:
    """Establish a deck-legal 1..4 cadence from the downbeat majority vote.

    Raises when ``beat_times`` is not strictly ascending or when a downbeat is
    not one of the beats. Fails closed when `vote_bar_phase` agreement is below
    ``floor``. ``n_downbeats_thinned`` keeps its payload name and now counts
    the off-phase doubles the vote left out.
    """
    if bar_beats < 1:
        raise ValueError(f"bar_beats must be at least 1, got {bar_beats}")

    beats = _ascending_floats(beat_times)
    downbeats = sorted(float(t) for t in downbeat_times)
    if not beats:
        return BarPhase([], True, REASON_NO_BEATS, 0, len(downbeats), 0, 0, 0, 0)

    anchors = _downbeat_indices(beats, downbeats, tolerance_s)
    closed, open_bar = _bar_lengths(anchors, len(beats)) if anchors else ([], 0)
    over, under, longest = _bar_anomalies(closed, open_bar, bar_beats)

    if not anchors:
        return BarPhase(
            [],
            True,
            REASON_NO_DOWNBEAT_ANCHOR,
            len(beats),
            0,
            0,
            over,
            under,
            longest,
        )

    vote = vote_bar_phase(anchors, bar_beats)
    chosen, agreement = vote.chosen, vote.agreement
    n_disagree, n_thinned = vote.n_disagreements, vote.n_doubles

    if agreement < floor:
        return BarPhase(
            [],
            True,
            REASON_BAR_PHASE_BELOW_FLOOR,
            len(beats),
            len(anchors),
            0,
            over,
            under,
            longest,
            chosen_phase=chosen,
            phase_agreement=agreement,
            n_phase_disagreements=n_disagree,
            n_downbeats_thinned=n_thinned,
        )

    numbers = _lock_numbers(len(beats), chosen, bar_beats)
    first_bar_one = next(i for i, n in enumerate(numbers) if n == 1)
    return BarPhase(
        beat_numbers=numbers,
        bar_phase_unestablished=False,
        reason=None,
        n_beats=len(beats),
        n_downbeats=len(anchors),
        n_backprojected_beats=first_bar_one,
        n_bars_over_length=over,
        n_bars_under_length=under,
        max_bar_beats=longest,
        chosen_phase=chosen,
        phase_agreement=agreement,
        n_phase_disagreements=n_disagree,
        n_downbeats_thinned=n_thinned,
    )
