"""Migration 14 -> 15: backfill legacy ``track_fields`` sync stamps (#3101).

Rows that predate migration v6 carry NULL ``updated_at`` while still holding
a usable ``modified_at`` from the rekordbox import. CloudSync now orders
those rows by ``modified_at`` until this one-time backfill copies it into
``updated_at``; already-stamped rows are untouched.
"""
from __future__ import annotations

_V15: list[str] = [
    """
    UPDATE track_fields
    SET updated_at = modified_at
    WHERE updated_at IS NULL AND modified_at IS NOT NULL
    """,
]

__all__ = ["_V15"]
