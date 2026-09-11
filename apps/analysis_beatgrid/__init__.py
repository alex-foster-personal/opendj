"""Own beat-grid analysis: BPM, tempo-change tags and the uncertainty flags.

Lane `nav1-beatgrid` of specs/native-analysis-v1.md. This package holds the
BACKFILL PRODUCER LOGIC and nothing else: it turns beat times (from
`beat_this_runner.py`, or from any other source that emits beat times) into the
numbers the record will eventually carry. Writing `AnalysisRecord` rows is the
nav1-contract lane's job in wave 1, so nothing here touches the store.

The four pure modules are STDLIB ONLY on purpose, exactly as
`apps/analysis_bench/scorers/beatgrid.py` is. They are imported by pytest inside the repo
venv while the model itself lives in a throwaway PEP 723 environment carrying
torch. Keeping the policy code dependency-free is what lets the octave rule and
the changepoint detector and the bar-phase assignment be unit tested in the normal
fast test lane instead of behind a heavy optional marker.
"""

from apps.analysis.selection import register_serving_lane
from apps.analysis_beatgrid.bar_phase import (
    BAR_BEATS,
    BarPhase,
    assign_bar_phase,
)
from apps.analysis_beatgrid.bpm import (
    OCTAVE_RANGE_MAX_BPM,
    OCTAVE_RANGE_MIN_BPM,
    BpmEstimate,
    estimate_bpm,
    least_squares_bpm,
)
from apps.analysis_beatgrid.flags import PulseFlag, evaluate_pulse
from apps.analysis_beatgrid.tempo_change import (
    MIN_SEGMENT_BARS,
    TempoAnalysis,
    TempoMarker,
    detect_tempo_changes,
)

register_serving_lane("beatgrid")

__all__ = [
    "BAR_BEATS",
    "MIN_SEGMENT_BARS",
    "OCTAVE_RANGE_MAX_BPM",
    "OCTAVE_RANGE_MIN_BPM",
    "BarPhase",
    "BpmEstimate",
    "PulseFlag",
    "TempoAnalysis",
    "TempoMarker",
    "assign_bar_phase",
    "detect_tempo_changes",
    "estimate_bpm",
    "evaluate_pulse",
    "least_squares_bpm",
]
