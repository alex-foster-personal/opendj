"""Shared reader for the SYNC-04/05/06 diff CSVs.

Supersedes: the identical ``_load_cue_diff`` / ``_load_diff`` /
``_load_ratings_diff`` bodies in ``apps.sync.apply_cues``,
``apps.sync.apply_analysis`` and ``apps.sync.apply_ratings`` (deleted, no shim).
"""
from __future__ import annotations

import csv
from pathlib import Path


def load_diff_rows(path: Path) -> list[dict]:
    """Read a diff CSV; a path that does not exist yet is an empty diff."""
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fp:
        return list(csv.DictReader(fp))


#: Optional diff-CSV columns carrying the join provenance of a matched pair.
MATCH_CONFIDENCE_COLUMN = "match_confidence"
MATCH_SIGNALS_COLUMN = "match_signals"


def match_provenance(row: dict) -> dict[str, object]:
    """Return the match confidence, signals and method recorded on a diff row.

    ``match_method`` is ``isrc_exact`` when the ISRC signal fired,
    ``lower_confidence`` when another permitted signal set joined the pair,
    and ``unknown`` when the row carries no match columns.
    """
    raw_conf = (row.get(MATCH_CONFIDENCE_COLUMN) or "").strip()
    try:
        confidence: float | None = float(raw_conf) if raw_conf else None
    except ValueError:
        confidence = None
    raw_signals = row.get(MATCH_SIGNALS_COLUMN) or ""
    signals = [s.strip() for s in raw_signals.split("|") if s.strip()]
    if "isrc_exact" in signals:
        method = "isrc_exact"
    elif signals or confidence is not None:
        method = "lower_confidence"
    else:
        method = "unknown"
    return {
        "match_confidence": confidence,
        "match_signals": signals,
        "match_method": method,
    }


__all__ = [
    "MATCH_CONFIDENCE_COLUMN",
    "MATCH_SIGNALS_COLUMN",
    "load_diff_rows",
    "match_provenance",
]
