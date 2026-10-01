"""One rule for "Beat Sync may wander on this beatgrid" (GRIDFLAG-01).

The deck header badge and the library flag read the SAME rule, decided in
`docs/decisions/ADR-NEW-deck-beatgrid-source.md` (branch
`af--adr-beatgrid-source`):

  1. FLAG a grid when any beat interval is more than
     `uneven_interval_tolerance_s` (3 ms) from the grid's median interval.
     Millisecond storage alone moves an interval by up to 1 ms and the median
     by up to 1 ms more, so 3 ms is above storage noise.
  2. CLASSIFY a flagged grid with the constant-region recipe
     (`const_regions`, 25 ms, 16 beats) run on the grid's own beat times:
       - `suspect`        steady music under an uneven grid: the merged steady
                          span covers at least `steady_min_coverage` of the
                          track AND at least `steady_min_on_line` of ALL
                          beats sit within the region tolerance of that line.
       - `variable_tempo` anything else: the grid's tempo really moves, or no
                          16-beat steady region exists at all.
  3. A grid with fewer than two beats is `unknown`, never ok and never bad.

`ok` means evenly spaced. It does NOT mean the grid matches the audio: a
fixed-tempo grid with the wrong BPM or phase is perfectly even, and no
interval rule can see that.

The thresholds live in `THRESHOLDS`, the one config object. The TypeScript
mirror is `apps/webui/frontend/src/lib/rb/grid-quality.ts`; there is no shared
runtime between the two, so `tests/fixtures/grid_quality_conformance.json`
pins both: each side asserts its thresholds equal the fixture's and that every
fixture grid produces the fixture's class, numbers and message.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 classify_grid(): ok / suspect / variable_tempo / unknown.
    [if] a fixed-tempo grid stored to the millisecond [then] ok ⛔️ flagged
    [if] a steady grid has one beat moved 4 ms [then] suspect ⛔️ ok or variable
    [if] the tempo ramps through the track [then] variable_tempo ⛔️ suspect
    [if] fewer than two beats [then] unknown ⛔️ ok or suspect
  ✔︎ ✅ 🎯 grid_quality_message(): the deck and library wording, with numbers.
    [if] suspect [then] names uneven count, worst ms, on-line share ⛔️ bare label
    [if] variable_tempo [then] says tempo changes, never "uneven grid" ⛔️
    [if] any flagged class [then] ends "may wander on this track" ⛔️ "broken"

-Claude
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from apps.analysis_beatgrid import const_regions

GridClass = Literal["ok", "suspect", "variable_tempo", "unknown"]

GRID_CLASSES: tuple[GridClass, ...] = ("ok", "suspect", "variable_tempo", "unknown")
#: The classes a user is warned about (and may dismiss).
FLAGGED_GRID_CLASSES: tuple[GridClass, ...] = ("suspect", "variable_tempo")

REASON_NO_GRID_DATA = "no_grid_data"
REASON_NOT_SCANNED = "not_scanned"
REASON_ANLZ_MISSING = "anlz_missing"
REASON_ANLZ_UNREADABLE = "anlz_unreadable"
REASON_NO_REKORDBOX_GRID = "no_rekordbox_grid"


# ---------------------------------------------------------------- config


@dataclass(frozen=True)
class GridQualityThresholds:
    """Every number the rule compares against. Change them HERE (and in the
    TypeScript mirror and the conformance fixture, which the tests force)."""

    #: An interval further than this from the median interval is uneven.
    uneven_interval_tolerance_s: float = 0.003
    #: Largest distance a beat may sit from a steady region's line.
    region_tolerance_s: float = const_regions.PHASE_TOLERANCE_S
    #: Fewest beat intervals a steady region needs (four bars).
    min_region_beats: int = const_regions.MIN_REGION_BEATS
    #: Share of the grid's time the merged steady span must cover.
    steady_min_coverage: float = const_regions.MIN_COVERAGE
    #: Share of ALL beats that must sit on the steady line. Provisional: the
    #: ADR placed it in a gap (0.58 to 0.72) across 19 real grids, by
    #: arithmetic, with nobody listening.
    steady_min_on_line: float = 0.7


THRESHOLDS = GridQualityThresholds()


def rule_version(thresholds: GridQualityThresholds = THRESHOLDS) -> str:
    """Changes whenever a threshold does, so a stored verdict goes stale."""
    digest = hashlib.sha256(
        json.dumps(asdict(thresholds), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"grid-quality-1:{digest[:12]}"


# ----------------------------------------------------------------- model


@dataclass(frozen=True)
class GridQuality:
    """One grid's verdict plus the numbers behind it."""

    grid_class: GridClass
    #: Why the class is `unknown`; None for every other class.
    reason: str | None
    interval_count: int
    #: Intervals more than the tolerance from the median interval.
    uneven_interval_count: int
    #: Largest interval deviation from the median, in ms (0 when none).
    worst_deviation_ms: float
    #: Track time of the worst interval's first beat.
    worst_at_sec: float
    #: 60 / median interval, or None without an interval.
    median_bpm: float | None
    #: Beats whose stored BPM differs from the beat before (0 for a fixed
    #: grid); None when the grid carries no per-beat BPM.
    tempo_marker_count: int | None
    #: Share of the grid's time the merged steady span covers (flagged only).
    steady_coverage: float | None
    #: Share of all beats within the region tolerance of the steady line.
    steady_on_line: float | None
    #: BPM of that steady line.
    steady_line_bpm: float | None


def unknown_quality(reason: str) -> GridQuality:
    """The `unknown` verdict for a grid that could not be judged, and why."""
    return GridQuality("unknown", reason, 0, 0, 0.0, 0.0, None, None, None, None, None)


# --------------------------------------------------------------- helpers


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _steady_line(
    times: Sequence[float], thresholds: GridQualityThresholds
) -> tuple[float, float, float] | None:
    """`(coverage, on_line, line_bpm)` of the one steady line, or None when no
    region of `min_region_beats` exists."""
    tolerance_s = thresholds.region_tolerance_s
    regions = const_regions.find_const_regions(times, tolerance_s)
    reference = const_regions._longest(regions, thresholds.min_region_beats)
    if reference is None:
        return None
    span = const_regions._extend(
        regions, reference, tolerance_s, (float("-inf"), float("inf"))
    )
    line = const_regions._line_for_span(
        times, span, True, tolerance_s, (times[0], times[-1])
    )
    period_s = 60.0 / line.bpm
    on_line_count = 0
    for beat_time in times:
        tick = round_half_up((beat_time - line.anchor_s) / period_s)
        if abs(beat_time - (line.anchor_s + tick * period_s)) <= tolerance_s:
            on_line_count += 1
    coverage = (span.end_s - span.start_s) / (times[-1] - times[0])
    return coverage, on_line_count / len(times), line.bpm


def round_half_up(value: float) -> int:
    """`Math.round` semantics, so Python and TypeScript agree on a tie."""
    return math.floor(value + 0.5)


def _fixed(value: float, digits: int) -> str:
    scale = 10**digits
    return f"{round_half_up(value * scale) / scale:.{digits}f}"


# ------------------------------------------------------------------ rule


def classify_grid(
    beat_times_s: Sequence[float],
    beat_bpms: Sequence[float] | None = None,
    *,
    thresholds: GridQualityThresholds = THRESHOLDS,
) -> GridQuality:
    """Classify one beatgrid from its beat times (seconds, in grid order).

    `beat_bpms` is the per-beat stored BPM (rekordbox PQTZ carries one); it
    only feeds `tempo_marker_count`, never the class.
    """
    times = [float(beat_time) for beat_time in beat_times_s]
    if len(times) < 2:
        return unknown_quality(REASON_NO_GRID_DATA)
    if beat_bpms is not None and len(beat_bpms) != len(times):
        raise ValueError(
            f"beat_bpms has {len(beat_bpms)} entries for {len(times)} beats"
        )
    interval_count = len(times) - 1
    intervals = [times[index + 1] - times[index] for index in range(interval_count)]
    median_s = _median(intervals)
    uneven_interval_count = 0
    worst_deviation_s = 0.0
    worst_at_sec = times[0]
    for index, interval in enumerate(intervals):
        deviation_s = abs(interval - median_s)
        if deviation_s <= thresholds.uneven_interval_tolerance_s:
            continue
        uneven_interval_count += 1
        if deviation_s > worst_deviation_s:
            worst_deviation_s = deviation_s
            worst_at_sec = times[index]
    tempo_marker_count: int | None = None
    if beat_bpms is not None:
        tempo_marker_count = sum(
            1
            for index in range(1, len(beat_bpms))
            if float(beat_bpms[index]) != float(beat_bpms[index - 1])
        )
    median_bpm = 60.0 / median_s if median_s > 0 else None

    if uneven_interval_count == 0:
        return GridQuality(
            "ok", None, interval_count, 0, 0.0, times[0], median_bpm,
            tempo_marker_count, None, None, None,
        )

    steady = _steady_line(times, thresholds)
    grid_class: GridClass
    if (
        steady is not None
        and steady[0] >= thresholds.steady_min_coverage
        and steady[1] >= thresholds.steady_min_on_line
    ):
        grid_class = "suspect"
    else:
        grid_class = "variable_tempo"
    return GridQuality(
        grid_class,
        None,
        interval_count,
        uneven_interval_count,
        worst_deviation_s * 1000,
        worst_at_sec,
        median_bpm,
        tempo_marker_count,
        steady[0] if steady is not None else None,
        steady[1] if steady is not None else None,
        steady[2] if steady is not None else None,
    )


# --------------------------------------------------------------- wording

UNKNOWN_REASON_TEXT: dict[str, str] = {
    REASON_NO_GRID_DATA: "no beatgrid is stored for this track",
    REASON_NOT_SCANNED: "this track has not been scanned yet",
    REASON_ANLZ_MISSING: "the rekordbox analysis file is missing",
    REASON_ANLZ_UNREADABLE: "the rekordbox analysis file could not be read",
    REASON_NO_REKORDBOX_GRID: "this track has no rekordbox analysis",
}


def uneven_intervals_phrase(quality: GridQuality) -> str:
    """The clause the deck badge has always shown for an uneven grid."""
    return (
        f"{quality.uneven_interval_count} of {quality.interval_count} beat intervals "
        f"are uneven (worst {_fixed(quality.worst_deviation_ms, 0)} ms off at "
        f"{_fixed(quality.worst_at_sec, 1)} s)"
    )


#: How every flagged message ends: sync may wander, the track is not broken.
FLAG_CONSEQUENCE = "Beat Sync may wander on this track"


def grid_flag_clause(quality: GridQuality) -> str:
    """What is uneven about a FLAGGED grid, without prefix or consequence."""
    if quality.grid_class == "suspect":
        if quality.steady_on_line is None or quality.steady_line_bpm is None:
            raise ValueError("a suspect grid always carries its steady line")
        return (
            f"{uneven_intervals_phrase(quality)}, but "
            f"{_fixed(quality.steady_on_line * 100, 0)}% of beats sit on one "
            f"{_fixed(quality.steady_line_bpm, 2)} BPM line (the grid is uneven, "
            "the music is not)"
        )
    if quality.grid_class == "variable_tempo":
        markers = quality.tempo_marker_count
        marker_text = ""
        if markers is not None:
            marker_text = f", {markers} tempo marker{'' if markers == 1 else 's'}"
        return (
            "tempo changes through this track: "
            f"{uneven_intervals_phrase(quality)}{marker_text}"
        )
    raise ValueError(f"not a flagged grid class: {quality.grid_class!r}")


def grid_quality_message(
    quality: GridQuality, *, thresholds: GridQualityThresholds = THRESHOLDS
) -> str:
    """One line for the hover title: what was measured, with the numbers."""
    if quality.grid_class == "unknown":
        if quality.reason is None:
            raise ValueError("an unknown grid always says why")
        return f"Beatgrid not checked: {UNKNOWN_REASON_TEXT[quality.reason]}"
    if quality.grid_class == "ok":
        return (
            f"Beatgrid: all {quality.interval_count} beat intervals are within "
            f"{_fixed(thresholds.uneven_interval_tolerance_s * 1000, 0)} ms of the "
            "median (evenly spaced; not checked against the audio)"
        )
    if quality.grid_class in FLAGGED_GRID_CLASSES:
        return f"Beatgrid: {grid_flag_clause(quality)} - {FLAG_CONSEQUENCE}"
    raise ValueError(f"unhandled grid class: {quality.grid_class!r}")


__all__ = [
    "FLAGGED_GRID_CLASSES",
    "FLAG_CONSEQUENCE",
    "GRID_CLASSES",
    "REASON_ANLZ_MISSING",
    "REASON_ANLZ_UNREADABLE",
    "REASON_NOT_SCANNED",
    "REASON_NO_GRID_DATA",
    "REASON_NO_REKORDBOX_GRID",
    "THRESHOLDS",
    "UNKNOWN_REASON_TEXT",
    "GridClass",
    "GridQuality",
    "GridQualityThresholds",
    "classify_grid",
    "grid_flag_clause",
    "grid_quality_message",
    "round_half_up",
    "rule_version",
    "uneven_intervals_phrase",
    "unknown_quality",
]
