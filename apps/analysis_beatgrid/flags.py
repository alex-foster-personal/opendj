"""The `no_trackable_pulse` uncertainty flag for the beat-grid lane.

WHY THIS EXISTS AS A FIRST-CLASS RESULT RATHER THAN AN EMPTY LIST. Round 0
measured beat_this emitting NOTHING on 6 of 137 dynamic-tempo tracks and under
half the reference beats on another 42 (specs/beat-mapping-bench.md). Its
threshold-based peak picker declines rather than guessing, which is a virtue,
but only if the decline survives to the reader. An empty `beats` array with
`status: ok` is a contract violation, not a state
(specs/native-analysis-v1.md section 3): the grid controls must go inert with a
tooltip naming the reason, and they must NOT quietly fall back to rekordbox's
grid in its place.

TWO INDEPENDENT SYMPTOMS, EITHER SUFFICIENT. The activation peak is the
model's own confidence: Beat This! keeps a frame only when its probability
exceeds 0.5, so a track whose activations never clear that is one the model
declined outright, and the peak says so even when a handful of stray beats
survived. The beat count is the coverage symptom: fewer than 8 beats in a
window cannot support a fit, a bar assignment, or a changepoint, whatever the
activations looked like. They fail in different directions, so both are
checked and the reason names which fired.

NO HIDDEN DEFAULT FOR THE ACTIVATION PEAK. An analyzer that cannot report its
peak must not be silently treated as confident. `activation_peak=None` raises,
because the alternative is a flag that reads healthy for the one case it was
built to catch.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# Beat This! keeps a frame only when its beat probability exceeds this, so a
# track whose peak activation never reaches it produced no beats by the model's
# own rule. Named here rather than inlined because it is the lever the research
# survey identifies for the dynamic-tempo under-gridding, and lowering it is a
# round-level experiment, not a per-call argument.
ACTIVATION_PEAK_THRESHOLD = 0.5

# Below this, there is nothing to fit a tempo to, assign a bar to, or segment.
MIN_BEATS = 8

REASON_TOO_FEW_BEATS = "too_few_beats"
REASON_ACTIVATION_BELOW_THRESHOLD = "activation_below_threshold"
REASON_NO_DOWNBEAT_ANCHOR = "no_downbeat_anchor"


@dataclass(frozen=True)
class PulseFlag:
    no_trackable_pulse: bool
    reason: str | None
    n_beats: int
    activation_peak: float
    n_downbeats: int | None = None


def evaluate_pulse(
    beat_times: Sequence[float],
    activation_peak: float | None,
    downbeat_times: Sequence[float] | None = None,
    *,
    threshold: float = ACTIVATION_PEAK_THRESHOLD,
    min_beats: int = MIN_BEATS,
) -> PulseFlag:
    """Decide whether this track has a pulse worth publishing a grid for.

    Raises when `activation_peak` is None: see the module docstring. Order is
    cause before consequence: "the model was never confident" is reported ahead
    of "and therefore emitted too few beats", which follows from it.

    A grid with beats but NO DOWNBEAT is failed rather than published. Measured
    on the committed round-0 artifact: stable id
    ff92cb1da9096b38d47050e448b3131e3922d7db has 22 beats and zero downbeats,
    1 of 337 fixtures. Without an anchor there is no bar-1, so `beat_numbers`
    comes back empty and nothing downstream can place a 1-to-4 phase. Serving
    that as `status: ok` would give quantize and Beat Sync a grid they cannot
    use while telling them it is healthy (Codex P1 on PR #1514, verified).
    `downbeat_times=None` means the caller has no downbeat concept to report
    and skips the check; an empty SEQUENCE means the analyzer looked and found
    none, which is the failure.
    """
    if activation_peak is None:
        raise ValueError(
            "activation_peak is required: an analyzer that cannot report its peak "
            "activation must not be treated as confident by default"
        )
    peak = float(activation_peak)
    n_beats = len(beat_times)
    n_downbeats = None if downbeat_times is None else len(downbeat_times)

    if peak < threshold:
        return PulseFlag(True, REASON_ACTIVATION_BELOW_THRESHOLD, n_beats, peak, n_downbeats)
    if n_beats < min_beats:
        return PulseFlag(True, REASON_TOO_FEW_BEATS, n_beats, peak, n_downbeats)
    if n_downbeats == 0:
        return PulseFlag(True, REASON_NO_DOWNBEAT_ANCHOR, n_beats, peak, n_downbeats)
    return PulseFlag(False, None, n_beats, peak, n_downbeats)
