"""Time-series structural audit, split out of
:mod:`apps.equivalence.single_source` (the file-size review gate). Purely
mechanical: :mod:`apps.equivalence.single_source` re-exports every name here,
so ``from apps.equivalence.single_source import audit_series`` (the existing
import shape) is unaffected.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from apps.equivalence.sources import MikEnergySegment

# Our storage base for every time series is milliseconds
# (docs/terminology-reference/time-series-vs-scalar.md).
#
# NOTE ON A TEST THAT LOOKED GOOD AND WAS VACUOUS: an earlier version asserted
# that the rounding residual |x*1000 - round(x*1000)| stayed under 0.5 ms. It
# cannot exceed 0.5 by construction, so the assertion could never fail and
# proved nothing. The residual is still reported, as information, but the
# checks that GATE the verdict are the ones that can actually fail:
#
#   * a span shorter than half a millisecond rounds to length 0 and VANISHES;
#   * rounding can push one span's end past the next span's start, breaking
#     non-overlap AFTER conversion even though the seconds did not overlap.
#
# Both are checked on the rounded integer milliseconds, which is what we would
# actually store.
MS_ROUNDING_BUDGET_MS = 0.5
"""Reported for information only. Bounded by 0.5 by construction, so it is
NOT evidence on its own -- see the note above."""

MS_CONVERSION_RULE = (
    "Convert BOUNDARIES, not durations: start_ms = round(start_s * 1000) and "
    "end_ms = round((start_s + length_s) * 1000), then length_ms = end_ms - "
    "start_ms. Rounding start and length INDEPENDENTLY rounds two numbers that "
    "must stay consistent, and round(a) + round(b) != round(a + b), so it "
    "injects up to 1 ms of overlap or gap at every segment boundary."
)
"""The prescription a loader must follow. MEASURED consequence of ignoring it
is reported per run, because on MIK's real data it is not a rare edge case."""



@dataclass
class SeriesStructure:
    """Structural audit of a time series. Every number is measured."""

    tracks: int
    segments: int
    min_segments_per_track: int
    max_segments_per_track: int
    non_monotonic_tracks: int
    overlapping_tracks: int
    non_positive_length_segments: int
    negative_start_segments: int
    min_start_s: float
    max_end_s: float
    max_ms_rounding_residual_ms: float
    min_length_s: float
    vanishing_segments_after_ms_rounding: int
    overlapping_tracks_naive_ms: int
    overlapping_tracks_boundary_ms: int
    gap_tracks: int
    value_histogram: dict[str, int] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def sound(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict[str, Any]:
        return {
            "tracks": self.tracks,
            "segments": self.segments,
            "segments_per_track": {
                "min": self.min_segments_per_track,
                "max": self.max_segments_per_track,
            },
            "non_monotonic_tracks": self.non_monotonic_tracks,
            "overlapping_tracks": self.overlapping_tracks,
            "non_positive_length_segments": self.non_positive_length_segments,
            "negative_start_segments": self.negative_start_segments,
            "min_start_s": self.min_start_s,
            "max_end_s": self.max_end_s,
            "max_ms_rounding_residual_ms": self.max_ms_rounding_residual_ms,
            "ms_rounding_residual_note": (
                "bounded by 0.5 ms by construction, so this number alone is not "
                "evidence; the gating checks are the two below, which can fail"
            ),
            "min_length_s": self.min_length_s,
            "vanishing_segments_after_ms_rounding": (
                self.vanishing_segments_after_ms_rounding
            ),
            "overlapping_tracks_naive_ms": self.overlapping_tracks_naive_ms,
            "overlapping_tracks_boundary_ms": self.overlapping_tracks_boundary_ms,
            "ms_conversion_rule": MS_CONVERSION_RULE,
            "gap_tracks": self.gap_tracks,
            "value_histogram": self.value_histogram,
            "findings": self.findings,
            "violations": self.violations,
            "sound": self.sound,
        }


@dataclass
class _TrackMeasurement:
    """One track's contribution to :class:`SeriesStructure`, isolated so the
    per-segment reducer does not count against :func:`audit_series` itself."""

    non_monotonic: bool
    non_positive: int
    negative_start: int
    out_of_scale: set[float]
    value_counts: Counter[str]
    min_length: float
    residual: float
    vanishing: int
    overlapping: bool
    gap_occurrences: int
    overlaps_naive: bool
    overlaps_boundary: bool
    low: float
    high: float


@dataclass
class _TrackAccumulator:
    """Mutable running state for one track, folded segment by segment."""

    non_positive: int = 0
    negative_start: int = 0
    out_of_scale: set[float] = field(default_factory=set)
    values: Counter[str] = field(default_factory=Counter)
    min_length: float = float("inf")
    residual: float = 0.0
    vanishing: int = 0
    overlapping: bool = False
    gap_occurrences: int = 0
    overlaps_naive: bool = False
    overlaps_boundary: bool = False
    previous_end: float | None = None
    previous_naive_end_ms: int | None = None
    previous_boundary_end_ms: int | None = None


def _check_ms_rounding_hazard(acc: _TrackAccumulator, segment: MikEnergySegment) -> None:
    """Update the vanishing/naive-overlap/boundary-overlap counters for one
    segment's two candidate millisecond conversions.

    NAIVE rounds start and length independently; BOUNDARY rounds both
    absolute times and derives the length. Comparing them is what turns
    "rounding is lossy" into an actionable instruction.
    """
    start_ms = round(segment.start_s * 1000.0)
    naive_length_ms = round(segment.length_s * 1000.0)
    boundary_end_ms = round(segment.end_s * 1000.0)
    if naive_length_ms <= 0 or boundary_end_ms - start_ms <= 0:
        acc.vanishing += 1
    if acc.previous_naive_end_ms is not None and start_ms < acc.previous_naive_end_ms:
        acc.overlaps_naive = True
    if (
        acc.previous_boundary_end_ms is not None
        and start_ms < acc.previous_boundary_end_ms
    ):
        acc.overlaps_boundary = True
    acc.previous_naive_end_ms = start_ms + naive_length_ms
    acc.previous_boundary_end_ms = boundary_end_ms


def _measure_segment(
    acc: _TrackAccumulator,
    segment: MikEnergySegment,
    value_range: tuple[float, float],
    overlap_tolerance_s: float,
) -> bool:
    """Fold one segment into ``acc``. Returns True when the track has hit an
    overlap and the caller should stop scanning further segments."""
    if segment.length_s <= 0:
        acc.non_positive += 1
    if segment.start_s < 0:
        acc.negative_start += 1
    if not value_range[0] <= segment.energy <= value_range[1]:
        acc.out_of_scale.add(segment.energy)
    acc.values[f"{segment.energy:g}"] += 1
    acc.min_length = min(acc.min_length, segment.length_s)
    for seconds in (segment.start_s, segment.length_s):
        exact_ms = seconds * 1000.0
        acc.residual = max(acc.residual, abs(exact_ms - round(exact_ms)))
    _check_ms_rounding_hazard(acc, segment)
    stop = False
    if acc.previous_end is not None:
        if segment.start_s < acc.previous_end - overlap_tolerance_s:
            acc.overlapping = True
            stop = True
        elif segment.start_s > acc.previous_end + overlap_tolerance_s:
            acc.gap_occurrences += 1
    acc.previous_end = segment.end_s
    return stop


def _measure_track(
    rows: list[MikEnergySegment],
    value_range: tuple[float, float],
    overlap_tolerance_s: float,
) -> _TrackMeasurement:
    ordered = sorted(rows, key=lambda s: s.start_s)
    non_monotonic = [s.start_s for s in rows] != [s.start_s for s in ordered]
    acc = _TrackAccumulator()
    for segment in ordered:
        if _measure_segment(acc, segment, value_range, overlap_tolerance_s):
            break
    return _TrackMeasurement(
        non_monotonic=non_monotonic,
        non_positive=acc.non_positive,
        negative_start=acc.negative_start,
        out_of_scale=acc.out_of_scale,
        value_counts=acc.values,
        min_length=acc.min_length,
        residual=acc.residual,
        vanishing=acc.vanishing,
        overlapping=acc.overlapping,
        gap_occurrences=acc.gap_occurrences,
        overlaps_naive=acc.overlaps_naive,
        overlaps_boundary=acc.overlaps_boundary,
        low=ordered[0].start_s,
        high=ordered[-1].end_s,
    )


@dataclass
class _SeriesTotals:
    non_positive: int
    overlapping: int
    non_monotonic: int
    out_of_scale: set[float]
    vanishing: int
    overlapping_boundary_ms: int
    overlapping_naive_ms: int
    residual: float
    negative_start: int
    gapped: int


def _apply_violations_and_findings(
    structure: SeriesStructure,
    totals: _SeriesTotals,
    value_range: tuple[float, float],
) -> None:
    """Violations BLOCK the verdict. Findings are recorded, not fatal."""
    non_positive = totals.non_positive
    overlapping = totals.overlapping
    non_monotonic = totals.non_monotonic
    out_of_scale = totals.out_of_scale
    vanishing = totals.vanishing
    overlapping_boundary_ms = totals.overlapping_boundary_ms
    overlapping_naive_ms = totals.overlapping_naive_ms
    residual = totals.residual
    negative_start = totals.negative_start
    gapped = totals.gapped
    if non_positive:
        structure.violations.append(
            f"{non_positive} segments have a length <= 0, which cannot be stored "
            f"as a span"
        )
    if overlapping:
        structure.violations.append(
            f"{overlapping} tracks have overlapping segments, so the series is "
            f"not a partition of the timeline and a reader would double-count"
        )
    if non_monotonic:
        structure.violations.append(
            f"{non_monotonic} tracks are not ordered by start time as read"
        )
    if out_of_scale:
        structure.violations.append(
            f"values outside the declared {value_range[0]:g}..{value_range[1]:g} "
            f"scale: {sorted(out_of_scale)[:5]}"
        )
    if vanishing:
        structure.violations.append(
            f"{vanishing} segments have a length under half a millisecond and "
            f"would round to length 0, i.e. VANISH, when stored in our "
            f"millisecond base"
        )
    if overlapping_boundary_ms:
        # The RECOMMENDED conversion breaks the invariant, so the source data
        # itself cannot be stored as non-overlapping spans. That is a real block.
        structure.violations.append(
            f"{overlapping_boundary_ms} tracks still overlap after the "
            f"boundary-derived millisecond conversion, so the series cannot be "
            f"stored as non-overlapping spans at millisecond resolution"
        )
    elif overlapping_naive_ms:
        # The source is fine; a NAIVE conversion would corrupt it. This is an
        # instruction to the loader, not a reason to reject the data.
        structure.findings.append(
            f"CONVERSION HAZARD, measured: rounding start and length "
            f"independently makes {overlapping_naive_ms} of {structure.tracks} "
            f"tracks ({overlapping_naive_ms / max(structure.tracks, 1):.1%}) "
            f"overlap by up to 1 ms, even though ZERO tracks overlap in "
            f"seconds. The boundary-derived conversion leaves 0 overlapping. "
            f"{MS_CONVERSION_RULE}"
        )
    if not vanishing:
        structure.findings.append(
            f"no span rounds away to zero: the shortest is "
            f"{structure.min_length_s:.4f} s. (The raw rounding residual is "
            f"{residual:.4f} ms, bounded by 0.5 ms by construction, so it proves "
            f"nothing on its own.)"
        )
    if negative_start:
        structure.findings.append(
            f"{negative_start} segments start BEFORE 0 s (minimum "
            f"{structure.min_start_s:.6f} s). This is MIK's analysis window "
            f"offset, not corruption: clamp to 0 on import and record that the "
            f"clamp happened. It is also why the analysed span is NOT a "
            f"container duration"
        )
    if gapped:
        structure.findings.append(
            f"{gapped} tracks have a gap between consecutive segments, so the "
            f"series is not guaranteed contiguous; a consumer must not assume "
            f"segment N ends where segment N+1 begins"
        )


def audit_series(
    segments: list[MikEnergySegment],
    *,
    value_range: tuple[float, float],
    overlap_tolerance_s: float = 1e-6,
) -> SeriesStructure:
    """Check the invariants a time series must hold to be storable as spans."""
    per_track: dict[int, list[MikEnergySegment]] = {}
    for segment in segments:
        per_track.setdefault(segment.song_pk, []).append(segment)

    non_monotonic = overlapping = gapped = 0
    non_positive = negative_start = 0
    vanishing = overlapping_naive_ms = overlapping_boundary_ms = 0
    residual = 0.0
    min_length = float("inf")
    lows: list[float] = []
    highs: list[float] = []
    values: Counter[str] = Counter()
    out_of_scale: set[float] = set()

    for rows in per_track.values():
        tm = _measure_track(rows, value_range, overlap_tolerance_s)
        non_monotonic += tm.non_monotonic
        non_positive += tm.non_positive
        negative_start += tm.negative_start
        out_of_scale |= tm.out_of_scale
        values.update(tm.value_counts)
        min_length = min(min_length, tm.min_length)
        residual = max(residual, tm.residual)
        vanishing += tm.vanishing
        overlapping += tm.overlapping
        gapped += tm.gap_occurrences
        overlapping_naive_ms += tm.overlaps_naive
        overlapping_boundary_ms += tm.overlaps_boundary
        lows.append(tm.low)
        highs.append(tm.high)

    counts = [len(rows) for rows in per_track.values()] or [0]
    structure = SeriesStructure(
        tracks=len(per_track),
        segments=len(segments),
        min_segments_per_track=min(counts),
        max_segments_per_track=max(counts),
        non_monotonic_tracks=non_monotonic,
        overlapping_tracks=overlapping,
        non_positive_length_segments=non_positive,
        negative_start_segments=negative_start,
        min_start_s=min(lows) if lows else 0.0,
        max_end_s=max(highs) if highs else 0.0,
        max_ms_rounding_residual_ms=residual,
        min_length_s=0.0 if min_length == float("inf") else min_length,
        vanishing_segments_after_ms_rounding=vanishing,
        overlapping_tracks_naive_ms=overlapping_naive_ms,
        overlapping_tracks_boundary_ms=overlapping_boundary_ms,
        gap_tracks=gapped,
        value_histogram=dict(sorted(values.items(), key=lambda kv: float(kv[0]))),
    )

    _apply_violations_and_findings(
        structure,
        _SeriesTotals(
            non_positive=non_positive,
            overlapping=overlapping,
            non_monotonic=non_monotonic,
            out_of_scale=out_of_scale,
            vanishing=vanishing,
            overlapping_boundary_ms=overlapping_boundary_ms,
            overlapping_naive_ms=overlapping_naive_ms,
            residual=residual,
            negative_start=negative_start,
            gapped=gapped,
        ),
        value_range,
    )
    return structure
