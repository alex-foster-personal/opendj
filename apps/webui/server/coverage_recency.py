"""Which tracks the user touched most recently, most recent first.

The auto-drain's analysis step is hours of work on a full library, so it
spends them where they are felt first: tracks on a deck right now, then tracks
loaded in a recorded set (``apps.sets`` event store), newest first. Everything
else keeps library order behind them.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 loaded decks, then set history, newest first, no duplicates
    [if] a track is on a deck [then] it precedes every set-history track
    [if] a track was loaded twice [then] it is listed once, at its newest load
    [if] no set was ever recorded [then] the history is empty, not an error
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

EVENTS_TABLE: str = "events"
HISTORY_LIMIT: int = 2000


def loaded_deck_ids(ui_mirror: Mapping[str, Any] | None) -> list[str]:
    decks = (ui_mirror or {}).get("decks")
    if not isinstance(decks, dict):
        return []
    return [
        deck["stable_id"]
        for deck in decks.values()
        if isinstance(deck, dict) and isinstance(deck.get("stable_id"), str)
    ]


def set_history_ids(sets_db: Path) -> list[str]:
    """Track ids from recorded sets, newest load first. Read-only."""
    if not sets_db.is_file():
        return []
    conn = sqlite3.connect(f"{sets_db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        has_events = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (EVENTS_TABLE,)
        ).fetchone()
        if has_events is None:
            return []
        rows = conn.execute(
            f"SELECT track_stable_id FROM {EVENTS_TABLE} "
            "WHERE track_stable_id IS NOT NULL "
            "GROUP BY track_stable_id ORDER BY MAX(wall_clock) DESC LIMIT ?",
            (HISTORY_LIMIT,),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def recent_track_ids(sets_db: Path, ui_mirror: Mapping[str, Any] | None) -> list[str]:
    return list(dict.fromkeys([*loaded_deck_ids(ui_mirror), *set_history_ids(sets_db)]))


__all__ = ["loaded_deck_ids", "recent_track_ids", "set_history_ids"]
