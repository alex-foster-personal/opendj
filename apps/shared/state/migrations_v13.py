"""Migration 12 -> 13: addressable playlist membership rows (LIBM-20 substrate).

Adds ``item_id`` and ``order_key`` to ``playlist_memberships`` so a one-track
``:add`` can INSERT a single row without rewriting neighbors. Does not change
the primary key ``(playlist_id, position)``; integer position remains a unique
slot while display order is ``order_key``.
"""
from __future__ import annotations

_V13: list[str] = [
    "ALTER TABLE playlist_memberships ADD COLUMN item_id TEXT",
    "ALTER TABLE playlist_memberships ADD COLUMN order_key TEXT",
    "UPDATE playlist_memberships SET item_id = lower(hex(randomblob(16))) "
    "WHERE item_id IS NULL",
    "UPDATE playlist_memberships SET order_key = printf('%08d', position) "
    "WHERE order_key IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_playlist_memberships_item_id "
    "ON playlist_memberships(item_id)",
]

__all__ = ["_V13"]
