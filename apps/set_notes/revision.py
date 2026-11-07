"""Optimistic revision checks for set notes (SETNOTE-6340c)."""

from __future__ import annotations

import sqlite3


class StaleRevision(Exception):
    def __init__(self, current_rev: int) -> None:
        super().__init__(f"stale base_rev; current rev is {current_rev}")
        self.current_rev = current_rev


def stale_rev_detail(current_rev: int) -> dict[str, object]:
    return {"error": "stale_rev", "current_rev": current_rev}


def current_rev(conn: sqlite3.Connection, scope: str) -> int:
    row = conn.execute("SELECT rev FROM set_notes WHERE scope = ?", (scope,)).fetchone()
    return int(row[0]) if row is not None else 0


def next_rev_for_base(base_rev: int, current: int) -> int:
    """Return the revision to write when ``base_rev`` matches ``current``."""
    if base_rev != current:
        raise StaleRevision(current)
    return current + 1


__all__ = ["StaleRevision", "current_rev", "next_rev_for_base", "stale_rev_detail"]
