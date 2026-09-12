"""v2 piecewise-constant tempo map over retained Beat This! activations.

This is not the v1 IBI tagger in ``tempo_change.py``. That module answers
whether one global BPM is a lie and where; this module emits rekordbox-style
multi-anchor tempo maps ``{at_s, beat_index, bpm}`` that a later consumer can
apply to rebuild beat times. The map is not on the record or ``/anlz`` yet.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from apps.analysis_beatgrid.activations import require_activations_for_fit
from apps.analysis_beatgrid.bpm import least_squares_bpm
from apps.analysis_beatgrid.tempo_change import detect_tempo_changes


@dataclass(frozen=True)
class TempoAnchor:
    """One piecewise-constant tempo section anchor."""

    at_s: float
    beat_index: int
    bpm: float
    confidence: float
    residual_rms_s: float


@dataclass(frozen=True)
class TempoMap:
    anchors: tuple[TempoAnchor, ...]


def _confidence_from_residual(residual_rms_s: float, bpm: float) -> float:
    # Same mapping as bpm._confidence_from_residual without the octave halving.
    period_s = 60.0 / bpm
    scatter = residual_rms_s / period_s if period_s > 0 else 1.0
    score = max(0.0, 1.0 - scatter / 0.25)
    return round(score, 4)


def _ls_intercept(segment_times: Sequence[float], bpm: float) -> float:
    n = len(segment_times)
    mean_i = (n - 1) / 2.0
    mean_t = sum(segment_times) / n
    period_s = 60.0 / bpm
    return mean_t - period_s * mean_i


def _fit_segment_anchor(
    beat_times: Sequence[float], lo: int, hi: int
) -> TempoAnchor:
    segment_times = list(beat_times[lo : hi + 1])
    fit = least_squares_bpm(segment_times)
    if fit is None:
        raise ValueError(
            f"tempo map fit refused: segment [{lo}, {hi}] has too few beats "
            "or a non-positive period"
        )
    bpm, residual_rms_s = fit
    at_s = _ls_intercept(segment_times, bpm)
    return TempoAnchor(
        at_s=at_s,
        beat_index=lo,
        bpm=bpm,
        confidence=_confidence_from_residual(residual_rms_s, bpm),
        residual_rms_s=residual_rms_s,
    )


def apply_tempo_map(tempo_map: TempoMap, n_beats: int) -> list[float]:
    """Piecewise-constant reconstruction: t(i) = at_s + (i - beat_index) * 60 / bpm."""
    if n_beats < 1:
        raise ValueError("n_beats must be at least 1")
    if not tempo_map.anchors:
        raise ValueError("tempo map has no anchors")

    anchors = tempo_map.anchors
    out: list[float] = []
    anchor_idx = 0
    for i in range(n_beats):
        while anchor_idx + 1 < len(anchors) and anchors[anchor_idx + 1].beat_index <= i:
            anchor_idx += 1
        anchor = anchors[anchor_idx]
        period_s = 60.0 / anchor.bpm
        out.append(anchor.at_s + (i - anchor.beat_index) * period_s)
    return out


def _beat_times_from_record(record_or_ref: Any) -> list[float]:
    beats: list[Any] | None = None
    if isinstance(record_or_ref, Mapping):
        beats = record_or_ref.get("beats")
        if beats is None:
            payload = record_or_ref.get("payload")
            if isinstance(payload, Mapping):
                beats = payload.get("beats")

    lanes = getattr(record_or_ref, "lanes", None)
    if beats is None and isinstance(lanes, Mapping):
        lane = lanes.get("beatgrid")
        if lane is not None:
            payload = getattr(lane, "payload", None)
            if isinstance(payload, Mapping):
                beats = payload.get("beats")

    if beats is None:
        raise ValueError(
            "dynamic grid fit refused: record carries no beat time series"
        )

    times: list[float] = []
    for entry in beats:
        if isinstance(entry, Mapping):
            times.append(float(entry["t"]))
        else:
            times.append(float(entry))
    return times


def fit_tempo_map(record_or_ref: Any, beat_times: Sequence[float]) -> TempoMap:
    """Fit a piecewise-constant tempo map over retained activations and beat times."""
    require_activations_for_fit(record_or_ref)
    analysis = detect_tempo_changes(beat_times)
    beats = list(beat_times)
    if not analysis.segments:
        if len(beats) < 2:
            raise ValueError("tempo map fit refused: fewer than two beat times")
        anchor = _fit_segment_anchor(beats, 0, len(beats) - 1)
        return TempoMap(anchors=(anchor,))

    anchors = tuple(
        _fit_segment_anchor(beats, lo, hi) for lo, hi, _ in analysis.segments
    )
    return TempoMap(anchors=anchors)


def fit_dynamic_grid(
    record_or_ref: Any, beat_times: Sequence[float] | None = None
) -> TempoMap:
    """Entry for the v2 fitter; requires retained activations and beat times."""
    require_activations_for_fit(record_or_ref)
    if beat_times is None:
        beat_times = _beat_times_from_record(record_or_ref)
    return fit_tempo_map(record_or_ref, beat_times)


__all__ = [
    "TempoAnchor",
    "TempoMap",
    "apply_tempo_map",
    "fit_dynamic_grid",
    "fit_tempo_map",
]
