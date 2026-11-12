"""One track's own BPM range for the beatgrid analysis (ANALYSIS-11723).

rekordbox lets a DJ re-analyze one track inside a BPM range of their choosing,
which is how a half-time drum and bass track or a 200 BPM hardcore track is
rescued. Open DJ stores that choice as the provenance-wrapped ``track_fields``
row ``analysis_bpm_range`` (source ``webui``), written by
``PUT /api/v1/tracks/{id}/analysis-range`` or ``opendj track analysis-range``.
The own beatgrid producer then picks the octave inside the track's range instead
of the global 70-180 band (``apps.analysis_beatgrid.bpm``), and stamps the range
on the grid it writes. Clearing it writes ``{"range": null}``.

This module owns no connection and opens no transaction.

-Claude
"""

from __future__ import annotations

import json
import sqlite3
from typing import Final, Literal, get_args

FIELD: Final = "analysis_bpm_range"
BLOB_VERSION: Final = 1
OWN_SOURCE: Final = "webui"
INVALID_CODE: Final = "ANALYSIS_RANGE_INVALID"

BpmRange = Literal["70-180", "60-120", "75-150", "85-170", "93-186", "100-200", "120-240"]
BPM_RANGES: Final[tuple[BpmRange, ...]] = get_args(BpmRange)
#: The global band; mirrors ``bpm.OCTAVE_RANGE_MIN_BPM``/``MAX_BPM`` (asserted in a test).
GLOBAL_RANGE: Final[BpmRange] = "70-180"


def bounds(bpm_range: BpmRange) -> tuple[float, float]:
    lo, hi = bpm_range.split("-")
    return float(lo), float(hi)


def parse(value: object) -> BpmRange:
    if value not in BPM_RANGES:
        raise ValueError(f"BPM range must be one of {', '.join(BPM_RANGES)}, got {value!r}")
    return value  # type: ignore[return-value]  # narrowed by the membership check


def blob(bpm_range: BpmRange | None) -> dict[str, object]:
    return {"v": BLOB_VERSION, "range": bpm_range}


def read(conn: sqlite3.Connection, stable_id: str) -> BpmRange | None:
    """The stored range; None when there is none, the table is absent, or it was cleared.

    Raises ValueError for a stored value this version cannot read.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='track_fields'").fetchone():
        return None
    row = conn.execute(
        "SELECT value_json FROM track_fields WHERE stable_id = ? AND field_name = ? AND deleted_at IS NULL",
        (stable_id, FIELD),
    ).fetchone()
    if row is None:
        return None
    value = json.loads(str(row[0]))
    stored = value.get("range") if isinstance(value, dict) else None
    if not (isinstance(value, dict) and value.get("v") == BLOB_VERSION) or (
        stored is not None and stored not in BPM_RANGES
    ):
        raise ValueError(f"{FIELD} for {stable_id} is not a v{BLOB_VERSION} range: {row[0]!r}")
    return stored


def needs_range_rerun(conn: sqlite3.Connection, stable_id: str, lane: str, record_json: str) -> bool:
    """True when a beatgrid record was made under a different range than the track's own now.

    A record carries ``lanes.beatgrid.payload.bpm_range`` only when it was made
    with an override; a record without it and a track without one agree, so no
    existing grid re-runs because this feature shipped.
    """
    if lane != "beatgrid":
        return False
    stored = read(conn, stable_id)
    wanted = list(bounds(stored)) if stored is not None else None
    payload = ((json.loads(record_json).get("lanes") or {}).get("beatgrid") or {}).get("payload") or {}
    made = payload.get("bpm_range")
    return (None if made is None else [float(x) for x in made]) != wanted


__all__ = [
    "BLOB_VERSION",
    "BPM_RANGES",
    "FIELD",
    "GLOBAL_RANGE",
    "INVALID_CODE",
    "OWN_SOURCE",
    "BpmRange",
    "blob",
    "bounds",
    "needs_range_rerun",
    "parse",
    "read",
]
