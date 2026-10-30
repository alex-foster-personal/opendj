"""Migration 24 -> 25: transition memory (MEMORY-01, ADR-0129).

``pair_transitions`` holds one saved transition per ordered pair of tracks: the plan that last ran between them, or
the one a person mixed by hand, as the plan document ``apps/autodj/plan.py`` parses. Most recent wins, so a retry that
sounds better replaces the plan it improved on.

Machine-local for now: it adds no write-token trigger and no wire change. Ids are stable ids, so syncing it later is
additive.
"""
from __future__ import annotations

PAIR_TRANSITION_SOURCES: tuple[str, ...] = ("planner", "captured", "edited")

_V25: list[str] = [
    """
    CREATE TABLE pair_transitions (
        out_track_id TEXT NOT NULL,
        in_track_id TEXT NOT NULL,
        plan_json TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('planner', 'captured', 'edited')),
        style TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        times_played INTEGER NOT NULL DEFAULT 0 CHECK (times_played >= 0),
        PRIMARY KEY (out_track_id, in_track_id)
    )
    """,
]

__all__ = ["PAIR_TRANSITION_SOURCES", "_V25"]
