"""Migration 19 -> 20: indexed CloudSync track identity lookups (issue #4397).

CLOUDSYNC-07 asks, for every incoming ``tracks`` row, which stored rows share
its ``content_hash``, ``audio_hash`` or ISRC. ``content_hash`` had no index and
the ISRC match compares ``upper(isrc)``, which ``idx_tracks_isrc`` cannot
serve, so both lookups scanned the table and a first sync of n tracks cost
O(n^2): 76.9 s at 10,000 tracks on agentbox.

The ISRC expression index is deliberately NOT partial: sqlite uses a partial
index for an OR arm only when that arm itself implies the index's WHERE, and
``upper(isrc) = ?`` does not, so a partial copy falls back to a scan.
``tests/cloudsync/test_track_identity_lookup_scale.py`` counts sqlite VM steps
per applied row and fails when either lookup stops using its index.
"""
from __future__ import annotations

_V20: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_tracks_content_hash ON tracks(content_hash) "
    "WHERE content_hash IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_tracks_isrc_upper ON tracks(upper(isrc))",
]

__all__ = ["_V20"]
