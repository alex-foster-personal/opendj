"""Pure beatgrid data-quality diagnostics (the browser "Err" column).

Detects PQTZ beats whose stored ``bpm`` field disagrees with the real
tempo implied by adjacent beat spacing (``60 / (beats[i+1].t - beats[i].t)``).
Rekordbox's own field can lie: "Proper Education" (Eric Prydz vs Floyd)
stores field bpm 120.34 at t=124.95s while the neighbouring beat interval
implies ~114.3 BPM - a real disagreement that can audibly jump tempo on a
Beat Sync seek near that point (see beat-sync-math.ts's windowed-median fix
for the playback side of this same bug).

A bare per-beat threshold cannot tell a noisy artifact from a real tempo
change: both produce a beat where field and interval disagree. The
distinguishing signal is run length - noise is 1-few beats surrounded by
otherwise-agreeing neighbors (a transient-detection artifact at a filter
sweep/drop; real captured data shows Proper Education's dirty ~120-130s
region runs up to 5 beats deep), while a real tempo change drifts the same
direction for many consecutive beats in a row. So a candidate only counts
when its run of consecutive same-direction disagreements is at most
MAX_RUN beats long.

Mirrors ``detectBeatgridIssue`` in
apps/webui/frontend/src/lib/rb/beat-sync-math.ts - keep both in sync if the
thresholds or shape ever change; there is no shared runtime between Python
(parses ANLZ) and TypeScript (renders the UI dot / unit-tests the math), so
this is a deliberate duplication of one small pure function.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

BeatgridIssueSeverity = Literal["warning", "error"]

#: Field vs. interval BPM disagreement above this is worth a look (orange).
WARN_DISAGREEMENT_BPM: float = 2.0
#: Above this, the disagreement is large enough to audibly jump tempo (red).
ERROR_DISAGREEMENT_BPM: float = 5.0
#: A run of this many or fewer consecutive same-direction outlier beats is
#: isolated noise; a longer run means the interval genuinely kept drifting
#: one way for many beats - a real sustained tempo change, not a data-quality
#: problem - so it is not flagged.
MAX_RUN: int = 6


def _interval_metrics(
    beats: Sequence[Mapping[str, Any]],
) -> tuple[int, list[float], list[float]]:
    interval_count = len(beats) - 1
    interval_bpms: list[float] = [float("nan")] * interval_count
    disagreements: list[float] = [0.0] * interval_count
    for i in range(interval_count):
        dt = float(beats[i + 1]["t"]) - float(beats[i]["t"])
        if dt <= 0:
            continue
        interval_bpm = 60.0 / dt
        interval_bpms[i] = interval_bpm
        disagreements[i] = float(beats[i]["bpm"]) - interval_bpm
    return interval_count, interval_bpms, disagreements


def _beat_direction(disagreements: list[float], i: int) -> int:
    d = disagreements[i]
    if abs(d) <= WARN_DISAGREEMENT_BPM:
        return 0
    return 1 if d > 0 else -1


def _direction_run_length(
    disagreements: list[float], interval_count: int, i: int,
) -> int:
    dir_ = _beat_direction(disagreements, i)
    start = i
    while start > 0 and _beat_direction(disagreements, start - 1) == dir_:
        start -= 1
    end = i
    while end < interval_count - 1 and _beat_direction(disagreements, end + 1) == dir_:
        end += 1
    return end - start + 1


def detect_beatgrid_issue(
    beats: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Worst ISOLATED field-vs-interval BPM disagreement in the grid.

    Returns None when every beat's field bpm is within tolerance of its own
    interval, every disagreement is part of a longer sustained drift, or the
    grid is too short to have an interval at all - all real "nothing to
    flag" states, never a guessed issue.
    """
    interval_count, interval_bpms, disagreements = _interval_metrics(beats)
    if interval_count < 1:
        return None

    worst: dict[str, Any] | None = None
    for i in range(interval_count):
        if _beat_direction(disagreements, i) == 0:
            continue
        if _direction_run_length(disagreements, interval_count, i) > MAX_RUN:
            continue
        disagreement_bpm = abs(disagreements[i])
        if worst is not None and disagreement_bpm <= worst["disagreement_bpm"]:
            continue
        severity: BeatgridIssueSeverity = (
            "error" if disagreement_bpm > ERROR_DISAGREEMENT_BPM else "warning"
        )
        worst = {
            "severity": severity,
            "at_sec": float(beats[i]["t"]),
            "field_bpm": float(beats[i]["bpm"]),
            "interval_bpm": interval_bpms[i],
            "disagreement_bpm": disagreement_bpm,
        }
    return worst


__all__ = [
    "WARN_DISAGREEMENT_BPM",
    "ERROR_DISAGREEMENT_BPM",
    "MAX_RUN",
    "BeatgridIssueSeverity",
    "detect_beatgrid_issue",
]
