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


__all__ = ["load_diff_rows"]
