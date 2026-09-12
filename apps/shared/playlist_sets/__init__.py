"""Playlist-set storage (SET-05).

SET-05 performance objects use their own ``playlist_sets`` tables, not
PLAY-01 ``play_orders`` and not SET-01 recorded ``sets``.
"""
from __future__ import annotations

from .schema import SCHEMA_VERSION, apply_playlist_set_migrations
from .store import (
    PlaylistSet,
    PlaylistSetEntry,
    PlaylistSetRun,
    create_playlist_set,
    list_playlist_sets,
    load_playlist_set,
    record_run,
)

__all__ = [
    "SCHEMA_VERSION",
    "PlaylistSet",
    "PlaylistSetEntry",
    "PlaylistSetRun",
    "apply_playlist_set_migrations",
    "create_playlist_set",
    "list_playlist_sets",
    "load_playlist_set",
    "record_run",
]
