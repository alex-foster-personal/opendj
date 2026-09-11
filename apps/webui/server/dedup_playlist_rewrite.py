"""Pure OpenDJ playlist membership rewrite helpers for duplicate merge.

The route owns PlaylistStore I/O, CAS, and the apply journal. This module only
rewrites stable-id lists and selects which playlists mention an alias.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any


def rewrite_items(
    items: Sequence[str], alias_ids: set[str], survivor: str,
) -> list[str]:
    """Replace every alias position with the survivor; keep duplicate slots."""
    return [survivor if stable_id in alias_ids else stable_id for stable_id in items]


def playlists_to_rewrite(
    playlists: Iterable[Any], alias_ids: set[str],
) -> list[Any]:
    """Playlists whose membership contains at least one alias stable id."""
    return [
        playlist
        for playlist in playlists
        if any(stable_id in alias_ids for stable_id in playlist.items)
    ]
