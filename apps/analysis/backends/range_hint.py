"""The own beatgrid backfill's BPM range: the track's own override, or the global band.

Split out of `own_beatgrid.py` to keep that module under the file-size ceiling.
The override is the user's own choice (`apps.shared.state.analysis_range`,
ANALYSIS-11723), never another program's analysis.

-Claude
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from apps.analysis_beatgrid.bpm import OCTAVE_RANGE_MAX_BPM, OCTAVE_RANGE_MIN_BPM
from apps.shared.state import analysis_range

log = logging.getLogger("apps.analysis.backends.own_beatgrid")


def track_bpm_range(stable_id: str, db_path: Path | None = None) -> tuple[float, float] | None:
    """The track's own range, or None. No state.db (a bench host) means none; a corrupt row raises."""
    from apps.shared.state.db import open_ro

    try:
        conn = open_ro(db_path)
    except (FileNotFoundError, sqlite3.OperationalError) as exc:
        log.info("no BPM range override for %s: state.db unavailable (%s)", stable_id, exc)
        return None
    try:
        stored = analysis_range.read(conn, stable_id)
    finally:
        conn.close()
    return analysis_range.bounds(stored) if stored is not None else None


def resolve_bpm_range(stable_id: str, db_path: Path | None = None) -> tuple[float, float]:
    """The range the producer analyzes this track in: its own, else the global band."""
    return track_bpm_range(stable_id, db_path) or (OCTAVE_RANGE_MIN_BPM, OCTAVE_RANGE_MAX_BPM)
