"""Migration 23 -> 24: drop ``idx_path_availability_checked`` (STATE-21).

The index on ``path_availability(checked_at)`` was never read. Every query on
the table goes through its primary key: ``path_index.bulk_lookup`` searches
``sqlite_autoindex_path_availability_1`` on ``(resolver_namespace,
logical_path)``, and ``path_index.upsert_rows`` finds its conflict row on that
same key; its ``checked_at < ?`` guard tests the one conflicting row and needs
no index. EXPLAIN QUERY PLAN on a copy of installed build 10's state.db (Tue 6
Oct 2026) shows neither statement choosing it.

Writing it was not free: every ``checked_at`` stamp deletes and re-inserts an
index entry, and on installed build 10 the index took 402 of 828 WAL frames
(and 6,613 of 9,010 frames together with the table on build 9). It was also the
index that needed ``REINDEX`` in the Mon 5 Oct 2026 preview corruption
recovery. ``IF EXISTS`` makes the step safe on a database that never had it.
"""
from __future__ import annotations

_V24: list[str] = [
    "DROP INDEX IF EXISTS idx_path_availability_checked",
]

__all__ = ["_V24"]
