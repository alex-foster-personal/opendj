"""Beatgrid verdict row model, split out of models.py to keep that module small."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class TempoStabilityRowOut(BaseModel):
    """The BPM Stability column (issue #7879), decided by the daemon.

    Read from the scan's stored tempo verdict; ``text`` and ``tone`` are the
    #7879 strings and colors, so the browser renders them and computes nothing.
    Absent (null) when the track's tempo was never judged: the column is blank.
    """

    verdict: Literal["steady", "drift", "free_intro", "changes"]
    largest_change_pct: float
    change_count: int
    text: str
    tone: Literal["none", "amber", "red", "deep_red"]
    title: str


class TempoCurveSegmentOut(BaseModel):
    start_s: float
    end_s: float
    bpm: float
    bpm_start: float
    bpm_end: float
    delta_pct: float
    kind: Literal["steady", "ramp", "jump", "unstable"]
    severity: float
    direction: Literal["faster", "slower", "none"]


class TempoCurveRowOut(BaseModel):
    """The row strip's tempo curve (row-assets ``tempo``): the stored segments.
    Null on a steady or never-judged track, so the strip paints nothing."""

    verdict: Literal["drift", "free_intro", "changes"]
    nominal_bpm: float
    largest_change_pct: float
    segments: list[TempoCurveSegmentOut]


class TrackAnalysisHintsOut(BaseModel):
    """Analyzer facts a row carries. ``dynamic_tempo`` is the stored tempo
    verdict is not steady; absent when the tempo was never judged."""

    dynamic_tempo: bool | None = None


class GridQualityRowOut(BaseModel):
    """A track row's stored beatgrid verdict (GRIDFLAG-02).

    Read back from the grid-quality store; a listing never parses a grid.
    ``unknown`` means nobody could judge the grid (``reason`` says why) and
    must never be drawn as ``ok`` or as a flag. ``message`` is the sentence
    the deck's Beat Sync badge shows for the same grid; null for ``ok``.
    """

    grid_class: Literal["ok", "suspect", "variable_tempo", "unknown"]
    reason: str | None = None
    # The user hid this track's flag (track field ``grid_flag_dismissed``).
    dismissed: bool = False
    message: str | None = None
    #: The BPM Stability column (issue #7879); null when tempo was never judged.
    tempo: TempoStabilityRowOut | None = None
