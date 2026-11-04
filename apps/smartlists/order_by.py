"""Shared ``order_by`` allowlist for repo validation and SQL evaluation."""

from __future__ import annotations

ORDER_BY_SQL: dict[str, str] = {
    "added_date asc": "tracks.created_at ASC",
    "added_date desc": "tracks.created_at DESC",
    "bpm asc": "_bpm ASC",
    "bpm desc": "_bpm DESC",
    "rating asc": "_rating ASC",
    "rating desc": "_rating DESC",
    "energy asc": "_energy ASC",
    "energy desc": "_energy DESC",
    "random": "RANDOM()",
}

ALLOWED_ORDER_BY: frozenset[str] = frozenset(ORDER_BY_SQL)

__all__ = ["ALLOWED_ORDER_BY", "ORDER_BY_SQL"]
