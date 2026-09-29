"""Runner output -> the `beatgrid` lane block of an `AnalysisRecord`.

THE SEAM BETWEEN THE PRODUCER AND THE RECORD. `beat_this_runner.py` emits beat
times, downbeat times and an activation peak in its own environment;
`apps/analysis/lanes.py` validates a lane payload with a fixed shape. This
module is the pure, stdlib-only function between them, so the whole conversion
is unit tested on synthetic beats without torch, a checkpoint or audio.

It is NOT `cli.py`. That module renders the same four pure policies into the
BENCH artifact schema (`bpm_raw`, `residual_rms_ms`, `octave_multiple`, the
bar anomaly counts), which is a different consumer with different fields. One
function serving both would have to satisfy the union of two contracts and
would drift from whichever one changed second.

## Three decisions this module makes, none of them silent

**A grid whose diagnostic bars are over- or under-length is re-phased, not failed.**
`assign_bar_phase` still counts anomalies for diagnostics, but served numbers
come from `lock_bar_phase`, which thins detector doubles, votes a majority
phase, and numbers every beat 1,2,3,4. A track fails only when that vote is
below the locked agreement floor (`bar_phase_below_floor`), not when a missed
downbeat would have broken the old nearest-preceding cadence
(`bar_length_anomaly` is no longer emitted in production).

**Per-beat BPM comes from the tempo SEGMENTS, not from adjacent intervals.**
A beat entry carries its local BPM and the deck plays against it, so deriving
it from one inter-beat interval would publish the beat tracker's own frame
quantization as tempo wobble. `detect_tempo_changes` already fits
piecewise-constant sections; each beat takes the mean tempo of the section it
falls in, which is the same curve `tempo_changes` describes. That local bpm
**is** the served tempo map on `/anlz`. Multi-anchor maps are trusted dynamic
grids (`static_grid_untrusted` is false on the record, omitted on `/anlz`).
One-anchor maps are the v1 static grid.

**The octave multiple is applied to every tempo this module publishes.** The
octave policy multiplies the least-squares fit to reach the published BPM, and
the segment means are fits of the same intervals at the raw level. Publishing
`bpm: 140` beside per-beat BPMs near 70, or a tempo marker reading 70 -> 72
under a 140 grid, would make the payload disagree with itself in exactly the
way `specs/native-analysis-v1.md` section 3 forbids ("the deck's public BPM
must agree with the beat intervals it plays against"). So `octave_multiple`
scales the per-beat BPM and both sides of every marker.

-Claude
"""
from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from apps.analysis_beatgrid.activations import FPS as ACTIVATIONS_FPS
from apps.analysis_beatgrid.bar_phase import BAR_BEATS, lock_bar_phase
from apps.analysis_beatgrid.bpm import estimate_bpm
from apps.analysis_beatgrid.flags import evaluate_pulse
from apps.analysis_beatgrid.grid_fit import (
    GRID_FIT_LINE,
    GRID_FIT_MODES,
    GRID_FIT_RAW,
    fit_grid,
)
from apps.analysis_beatgrid.tempo_change import detect_tempo_changes

#: A tempo fit that produced no straight line at all. `estimate_bpm` returns
#: None rather than guessing, and this is the reason that absence is published
#: under.
REASON_NO_TEMPO_FIT = "no_tempo_fit"

#: The bar cadence the deck enforces was broken by a missed or spurious
#: downbeat. See the module docstring.
REASON_BAR_LENGTH_ANOMALY = "bar_length_anomaly"

#: The runner recorded an exception for this track (decode failure, unreadable
#: file). Carried through verbatim so the record names what actually happened.
REASON_RUNNER_ERROR = "runner_error"


class LanePayloadError(ValueError):
    """The runner result cannot be converted at all, for a structural reason."""


@dataclass(frozen=True)
class BeatgridLane:
    """One track's beatgrid lane outcome, in the record contract's terms.

    ``status`` is only ever ``ok`` or ``failed`` here: ``missing`` describes a
    lane that has not run, which is a fact about the STORE and not something a
    producer that just ran can report about itself.
    """

    status: Literal["ok", "failed"]
    reason: str | None = None
    confidence: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _failed(reason: str) -> BeatgridLane:
    return BeatgridLane(status="failed", reason=reason, confidence=None, payload={})


def _cadence_breaks(numbers: Sequence[int], bar_beats: int) -> int:
    """How many adjacent pairs break the 1..bar_beats cadence the deck requires.

    Transcribed from `validateBeatGrid`'s own rule rather than derived from the
    bar-length counts: the deck's check is the one that decides whether this
    grid is usable, so it is the one asked.
    """
    breaks = 0
    for earlier, later in itertools.pairwise(numbers):
        expected = 1 if earlier == bar_beats else earlier + 1
        if later != expected:
            breaks += 1
    return breaks


def _segment_bpm_per_beat(
    n_beats: int, segments: Sequence[tuple[int, int, float]], octave_multiple: float
) -> list[float]:
    """Each beat's local BPM, taken from the tempo section its interval falls in.

    Segments are over INTERVAL indices, and there are ``n_beats - 1`` intervals,
    so the final beat takes the last interval's section: it closes that section
    rather than opening a new one.
    """
    if n_beats < 2:
        raise LanePayloadError(
            f"cannot derive per-beat BPM from {n_beats} beat(s); the pulse flag "
            "should have failed this track before it reached here"
        )
    if not segments:
        raise LanePayloadError(
            "tempo analysis produced no segments for a track with "
            f"{n_beats} beats; a track with intervals always has at least one"
        )
    per_beat: list[float] = []
    last_interval = n_beats - 2
    segment_index = 0
    for beat_index in range(n_beats):
        interval = min(beat_index, last_interval)
        while (
            segment_index + 1 < len(segments) and segments[segment_index][1] <= interval
        ):
            segment_index += 1
        bpm = segments[segment_index][2] * octave_multiple
        if not math.isfinite(bpm) or bpm <= 0:
            raise LanePayloadError(
                f"segment {segments[segment_index]} yields a per-beat BPM of "
                f"{bpm!r} at beat {beat_index}, which is not a tempo"
            )
        per_beat.append(round(bpm, 2))
    return per_beat


@dataclass(frozen=True)
class _PulseCheck:
    """What survives the guard chain: enough to assemble the `ok` payload."""

    beats: list[float]
    tempo: Any
    phase: Any


def _pulse_and_phase(
    result: Mapping[str, Any], *, threshold: float
) -> _PulseCheck | BeatgridLane:
    """The early guard chain, split out so its branches are counted apart from
    payload assembly (`build_beatgrid_lane` otherwise trips the CC ceiling).

    Returns a `BeatgridLane` the moment any guard fails, else a `_PulseCheck`
    carrying what `build_beatgrid_lane` needs to finish.
    """
    error = result.get("error")
    if error:
        return _failed(f"{REASON_RUNNER_ERROR}: {error}")

    for key in ("beats", "downbeats"):
        if key not in result:
            # `downbeats` is required, not defaulted to []. An explicitly EMPTY
            # list is a measured result (the model found no downbeat) and flows
            # on to `no_downbeat_anchor`; a MISSING key is producer contract
            # drift, and turning it into the same lane failure would persist a
            # `failed` record that `--only-missing` then declines to retry once
            # the protocol is fixed (Sol P1 BLOCKING, PR #1587).
            raise LanePayloadError(
                f"runner result carries no `{key}` key; got {sorted(result)}"
            )
    beats: list[float] = [float(t) for t in result["beats"]]
    downbeats: list[float] = [float(t) for t in result["downbeats"]]

    pulse = evaluate_pulse(
        beats, result.get("activation_peak"), downbeats, threshold=threshold
    )
    if pulse.no_trackable_pulse:
        assert pulse.reason is not None  # evaluate_pulse names every failure
        return _failed(pulse.reason)

    tempo = estimate_bpm(beats)
    if tempo is None:
        return _failed(REASON_NO_TEMPO_FIT)

    phase = lock_bar_phase(beats, downbeats)
    if phase.bar_phase_unestablished:
        assert phase.reason is not None
        return _failed(phase.reason)
    breaks = _cadence_breaks(phase.beat_numbers, BAR_BEATS)
    if breaks:
        raise LanePayloadError(
            f"lock_bar_phase produced {breaks} cadence break(s); programmer error"
        )

    return _PulseCheck(beats=beats, tempo=tempo, phase=phase)


def _stamp_activations(result: Mapping[str, Any], payload: dict[str, Any]) -> None:
    """Copy the retained-logits pointer onto an ok payload without stat()ing paths."""
    activations_npz = result.get("activations_npz")
    activations_blob = result.get("activations_blob")
    if activations_npz or activations_blob:
        fps_raw = result.get("fps", ACTIVATIONS_FPS)
        fps = int(fps_raw)
        if activations_blob is not None:
            payload["activations"] = {"blob": activations_blob, "fps": fps}
        else:
            payload["activations"] = {"npz": activations_npz, "fps": fps}
        return
    if result.get("n_frames") is not None or result.get("beats"):
        raise LanePayloadError(
            "runner result produced logits but carries no activations_npz or "
            "activations_blob pointer; producer contract drift"
        )


def build_beatgrid_lane(
    result: Mapping[str, Any], *, threshold: float, grid_fit: str = GRID_FIT_RAW
) -> BeatgridLane:
    """One `beat_this_runner.py` result into one lane block.

    ``threshold`` is the peak-keep probability the run was actually made at and
    is required: judging a run's activation peak against a different threshold
    from the one its peak picker used would reject downstream what the producer
    accepted upstream (the reasoning is `cli.analyze`'s, and it holds here).
    """
    if grid_fit not in GRID_FIT_MODES:
        raise LanePayloadError(f"grid_fit must be one of {GRID_FIT_MODES}, got {grid_fit!r}")
    if grid_fit == GRID_FIT_LINE:
        return _build_line_lane(result, threshold=threshold)
    checked = _pulse_and_phase(result, threshold=threshold)
    if isinstance(checked, BeatgridLane):
        return checked
    beats, tempo, phase = checked.beats, checked.tempo, checked.phase

    changes = detect_tempo_changes(beats)
    per_beat_bpm = _segment_bpm_per_beat(
        len(beats), changes.segments, tempo.octave_multiple
    )
    payload: dict[str, Any] = {
        "beats": [
            {"t": round(t, 5), "n": n, "bpm": bpm}
            for t, n, bpm in zip(beats, phase.beat_numbers, per_beat_bpm, strict=True)
        ],
        "bpm": round(tempo.bpm, 2),
        "bpm_confidence": tempo.confidence,
        "octave_reason": tempo.octave_reason,
        "first_downbeat_s": round(beats[phase.n_backprojected_beats], 5),
        "tempo_changes": [
            {
                "at_s": marker.at_s,
                "bpm_before": round(marker.bpm_before * tempo.octave_multiple, 2),
                "bpm_after": round(marker.bpm_after * tempo.octave_multiple, 2),
                "confidence": marker.confidence,
            }
            for marker in changes.markers
        ],
        "static_grid_untrusted": False,
    }
    if phase.phase_agreement is not None:
        payload["bar_phase_agreement"] = round(phase.phase_agreement, 4)
        payload["n_phase_disagreements"] = phase.n_phase_disagreements
    _stamp_activations(result, payload)
    return BeatgridLane(
        status="ok", reason=None, confidence=tempo.confidence, payload=payload
    )


def _build_line_lane(result: Mapping[str, Any], *, threshold: float) -> BeatgridLane:
    """`grid_fit="line"`: serve the fitted line instead of the model's peaks.

    Same guard chain up to the tempo fit (runner error, pulse, octave policy),
    then `grid_fit.fit_grid` replaces both the raw beats and `lock_bar_phase`.
    Per-beat BPM is the line's own (rounded) tempo, so the deck's local tempo
    and the beat spacing it plays against are the same number by construction.
    """
    error = result.get("error")
    if error:
        return _failed(f"{REASON_RUNNER_ERROR}: {error}")
    for key in ("beats", "downbeats"):
        if key not in result:
            raise LanePayloadError(f"runner result carries no `{key}` key; got {sorted(result)}")
    beats = [float(t) for t in result["beats"]]
    downbeats = [float(t) for t in result["downbeats"]]
    pulse = evaluate_pulse(beats, result.get("activation_peak"), downbeats, threshold=threshold)
    if pulse.no_trackable_pulse:
        assert pulse.reason is not None
        return _failed(pulse.reason)
    tempo = estimate_bpm(beats)
    if tempo is None:
        return _failed(REASON_NO_TEMPO_FIT)

    # The grid stays at the model's metrical level, as raw mode's beats do.
    # Re-rendering it at the octave policy's multiple was measured (round 4,
    # `line_round_offset_octave`) to halve 163-175 BPM tracks that rekordbox
    # keeps whole, so the published tempo here is the line's own.
    fit = fit_grid(beats, downbeats)
    if fit.reason is not None:
        return _failed(fit.reason)
    if _cadence_breaks(fit.beat_numbers, BAR_BEATS):
        raise LanePayloadError("grid_fit produced a cadence break; programmer error")

    line_bpm = [round(line.bpm, 2) for line in fit.lines]
    first_downbeat = next(t for t, n in zip(fit.beats, fit.beat_numbers, strict=True) if n == 1)
    markers = []
    for i in range(1, len(fit.lines)):
        owned = zip(fit.beats, fit.beat_lines, strict=True)
        at = next((t for t, owner in owned if owner == i), None)
        if at is None:
            continue
        markers.append({
            "at_s": round(at, 5),
            "bpm_before": line_bpm[i - 1],
            "bpm_after": line_bpm[i],
            "confidence": tempo.confidence,
        })
    payload: dict[str, Any] = {
        "beats": [
            {"t": round(t, 5), "n": n, "bpm": line_bpm[owner]}
            for t, n, owner in zip(fit.beats, fit.beat_numbers, fit.beat_lines, strict=True)
        ],
        "bpm": line_bpm[0] if len(line_bpm) == 1 else round(tempo.bpm, 2),
        "bpm_confidence": tempo.confidence,
        "octave_reason": (
            "grid_fit_model_level" if tempo.octave_multiple != 1 else tempo.octave_reason
        ),
        "first_downbeat_s": round(first_downbeat, 5),
        "tempo_changes": markers,
        "static_grid_untrusted": False,
        "grid_fit": {
            "mode": GRID_FIT_LINE,
            "offset_s": fit.offset_s,
            "octave_policy_multiple": tempo.octave_multiple,
            "segments": [
                {
                    "bpm": line_bpm[i],
                    "bpm_fitted": round(line.bpm_fitted, 4),
                    "round_step": line.round_step,
                    "n_inliers": line.n_inliers,
                    "n_outliers": line.n_outliers,
                    "residual_rms_ms": round(line.residual_rms_s * 1000.0, 2),
                }
                for i, line in enumerate(fit.lines)
            ],
        },
    }
    if fit.phase_agreement is not None:
        payload["bar_phase_agreement"] = round(fit.phase_agreement, 4)
    _stamp_activations(result, payload)
    return BeatgridLane(status="ok", reason=None, confidence=tempo.confidence, payload=payload)


__all__ = [
    "REASON_BAR_LENGTH_ANOMALY",
    "REASON_NO_TEMPO_FIT",
    "REASON_RUNNER_ERROR",
    "BeatgridLane",
    "LanePayloadError",
    "build_beatgrid_lane",
]
