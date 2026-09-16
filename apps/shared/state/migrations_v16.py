"""Migration 15 -> 16: durable backfill markers and changelog indexes (#3165).

Adds ``schema_meta_markers`` so one-time repairs (for example the v15
``track_fields`` stamp backfill) record completion and skip on later opens.
Adds the missing ``hub_changelog(table_name, row_pk)`` index; reuses the
existing ``idx_local_changelog_table`` ``IF NOT EXISTS`` form from v6.
"""
from __future__ import annotations

_V16: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS schema_meta_markers (
        marker     TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hub_changelog_table "
    "ON hub_changelog(table_name, row_pk)",
    "CREATE INDEX IF NOT EXISTS idx_local_changelog_table "
    "ON local_changelog(table_name, row_pk)",
]

__all__ = ["_V16"]
