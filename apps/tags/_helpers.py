"""Small shared helpers for ``apps.tags`` (sqlite chunking, JSON row columns)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path


def chunked(ids: list[str], size: int) -> Iterator[list[str]]:
    """Consecutive slices of at most ``size`` ids."""
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


def json_list_column(row: sqlite3.Row, column: str) -> list:
    """Decode a JSON list column from a sqlite row."""
    return json.loads(row[column])


def vendor_path_key(raw: str) -> str:
    """Normalise a filesystem path for vendor index lookup: expanduser + lowercase."""
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser()).casefold()
    except Exception:
        return raw.casefold()
