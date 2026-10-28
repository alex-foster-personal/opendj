"""One track's integrated loudness, read from the store apps/loudness writes (AGENT-31).

The loudness lane's scalars live in ``state.db`` ``analysis_projection``
(``field = 'loudness_lufs'``, written by ``apps.analysis.canonical``).
rekordbox has no loudness column, so there is nothing to substitute: the
answer is the projection row, or ``None`` with the reason it is missing.
Read only.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from pathlib import Path

from .models import TrackLoudnessOut

#: The projection columns this read needs (apps.analysis.canonical ``_write_projection_row``).
PROJECTION_COLUMNS = frozenset({"stable_id", "field", "value", "status", "reason"})
NO_ANALYSIS = "no loudness analysis for this track (apps/loudness has not measured it)"


def track_loudness(state_db_path: Path, stable_id: str) -> TrackLoudnessOut:
    """The track's integrated LUFS, or None with the status and reason (never a default loudness)."""
    # No store, or a store without the projection schema, is an error: only a missing ROW means "not measured".
    if not state_db_path.is_file():
        raise FileNotFoundError(f"loudness store missing: no state.db at {state_db_path}")
    conn = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(analysis_projection)")}
        missing = PROJECTION_COLUMNS - columns
        if missing:
            raise RuntimeError(f"loudness store {state_db_path} has no analysis_projection {sorted(missing)}")
        row = conn.execute(
            "SELECT value, status, reason FROM analysis_projection WHERE stable_id = ? AND field = 'loudness_lufs'",
            (stable_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return TrackLoudnessOut(lufs=None, status="missing", reason=NO_ANALYSIS)
    return _from_row(stable_id, *row)


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _reason_text(reason: object) -> bool:
    return isinstance(reason, str) and reason != ""


#: Every status the loudness projection may carry, and its ONE valid row shape: (value check, reason check).
#: Any other status, or a row that breaks its status's shape, is corrupt stored data and raises.
ROW_SHAPES: dict[str, tuple[Callable[[object], bool], Callable[[object], bool]]] = {
    "ok": (_finite_number, lambda reason: reason is None),
    "failed": (lambda value: value is None, _reason_text),
    "missing": (lambda value: value is None, _reason_text),
}


def _from_row(stable_id: str, value: object, status: object, reason: object) -> TrackLoudnessOut:
    """A projection row as served, or a raise: the row must match its status's shape in ROW_SHAPES exactly."""
    shape = ROW_SHAPES.get(status) if isinstance(status, str) else None
    if shape is None:
        raise ValueError(f"loudness_lufs projection for {stable_id} has unknown status {status!r}")
    value_ok, reason_ok = shape
    if not (value_ok(value) and reason_ok(reason)):
        raise ValueError(
            f"loudness_lufs projection for {stable_id} is not a valid {status} row: value {value!r}, reason {reason!r}"
        )
    if status == "ok":
        return TrackLoudnessOut(lufs=float(value), status="ok", reason=None)  # type: ignore[arg-type]
    return TrackLoudnessOut(lufs=None, status=status, reason=str(reason))  # type: ignore[arg-type]
