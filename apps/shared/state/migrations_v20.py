"""Migration 19 -> 20: bounded playlist membership reads (LIBM-132, issue #3963).

``POST .../items:add`` reads only the neighbors' order_keys and the requested
ids' existing copies. Without an index sqlite answered both by walking every
live membership row of the playlist and sorting it (``USE TEMP B-TREE FOR
ORDER BY``), 25 ms at 10,042 members on agentbox, so the add stayed O(members)
in C even after it stopped materializing rows in Python.

The order index expression MUST stay textually identical to
``apps.webui.server.playlist_add.MEMBERSHIP_ORDER_BY``: sqlite only uses an
expression index for an ORDER BY that matches it.
``tests/test_playlist_add_constant_time.py`` reads the query plan of the SQL
the add really runs and fails when the index stops being used.
"""
from __future__ import annotations

_V20: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_playlist_memberships_live_order "
    "ON playlist_memberships("
    "playlist_id, COALESCE(order_key, printf('%08d', position)), position"
    ") WHERE deleted_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_playlist_memberships_live_stable_id "
    "ON playlist_memberships(playlist_id, stable_id) WHERE deleted_at IS NULL",
]

__all__ = ["_V20"]
