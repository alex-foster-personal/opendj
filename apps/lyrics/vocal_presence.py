"""Stem vocal-coverage -> the no-lyrics verdict. THE canonical thresholds.

Coverage is the share of a track's duration carrying separated vocal-stem
energy. scripts/vocal_region_worker.py computes the RMS ratio envelope,
hysteresis (ON 0.10 / OFF 0.05), merge and minimum length; this module only
bands that number and never re-derives it.

Public calibration uses MTG-Jamendo electronic tracks with unanimous
voice/instrumental labels. Private follow-up inputs and their regression
module are withheld from this public copy, so private calibration acceptance
cannot be reproduced here. The shipped thresholds remain unchanged.

The conservative no-lyrics band avoids silently disabling karaoke on vocal
tracks. The sparse band requires other evidence such as ASR word counts or
licensed instrumental declarations; above that band, treat the track as
vocal. Import these canonical thresholds instead of copying them elsewhere.
"""

from __future__ import annotations

from typing import Literal

NO_LYRICS_MAX_COVERAGE: float = 12.5
SPARSE_MAX_COVERAGE: float = 25.0

CoverageVerdict = Literal["no-lyrics", "sparse", "vocal"]


def coverage_verdict(coverage_pct: float) -> CoverageVerdict:
    """Band one track's stem vocal coverage.

    'no-lyrics'  confident enough to disable karaoke and skip lyric fetching.
    'sparse'     ambiguous: chants, chops, one-line hooks. Other evidence
                 (ASR word count, licensed instrumental flag, ears) decides;
                 never silently dropped.
    'vocal'      treat as a lyrics track.
    """
    if not 0.0 <= coverage_pct <= 100.0:
        raise ValueError(f"coverage_pct out of range: {coverage_pct}")
    if coverage_pct <= NO_LYRICS_MAX_COVERAGE:
        return "no-lyrics"
    elif coverage_pct <= SPARSE_MAX_COVERAGE:  # noqa: RET505 - explicit elif is house style
        return "sparse"
    else:
        return "vocal"
