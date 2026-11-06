"""Migration 28 -> 29: live membership lookup by stable_id (issue #8785).

``SqliteBackend.list_playlists`` runs a correlated ``NOT EXISTS`` over
``playlist_memberships.stable_id`` on every tree paint. The v22 live indexes
both lead with ``playlist_id``, so that subquery scanned per track. A partial
``(stable_id, playlist_id)`` index makes it a ``SEARCH``. Same index also
serves ``list_track_playlists``. Review thread r4476065740 on PR #8776.
No wire bump: a non-unique index is not a wire change.
"""

from __future__ import annotations

_V29: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_playlist_memberships_live_by_stable "
    "ON playlist_memberships(stable_id, playlist_id) WHERE deleted_at IS NULL",
]

__all__ = ["_V29"]
