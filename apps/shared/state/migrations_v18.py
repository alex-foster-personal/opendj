"""Migration 17 -> 18: persisted path availability index (issue #1037).

Adds ``path_availability``: resolver-namespaced disk-truth cache for rekordbox
listing hydration. Survives process restarts; request handlers serve index
rows immediately and refresh stale or missing paths under per-request budgets.
"""
from __future__ import annotations

_V18: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS path_availability (
        resolver_namespace TEXT NOT NULL,
        logical_path       TEXT NOT NULL,
        materialised_size  INTEGER,
        checked_at         TEXT NOT NULL,
        PRIMARY KEY (resolver_namespace, logical_path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_path_availability_checked "
    "ON path_availability(checked_at)",
]

__all__ = ["_V18"]
