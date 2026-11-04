"""Playlist-set storage (SET-05) and set routes (SET-6586).

SET-05 performance objects use their own ``playlist_sets`` tables, not
PLAY-01 ``play_orders`` and not SET-01 recorded ``sets``.
"""
from __future__ import annotations

from .components import (
    DEFAULT_SLOTS,
    Component,
    ComponentInUseError,
    Route,
    RouteListing,
    borrowing_playlists,
    create_component,
    delete_component,
    find_set_for_route,
    list_borrowed_components,
    list_components,
    list_routes,
    list_slots,
    load_component,
    update_component,
)
from .schema import SCHEMA_VERSION, apply_playlist_set_migrations
from .store import (
    PlaylistSet,
    PlaylistSetEntry,
    PlaylistSetRun,
    create_playlist_set,
    list_playlist_sets,
    load_playlist_set,
    load_run_entries,
    record_run,
    update_set_parts,
)

__all__ = [
    "DEFAULT_SLOTS",
    "SCHEMA_VERSION",
    "Component",
    "ComponentInUseError",
    "PlaylistSet",
    "PlaylistSetEntry",
    "PlaylistSetRun",
    "Route",
    "RouteListing",
    "apply_playlist_set_migrations",
    "borrowing_playlists",
    "create_component",
    "create_playlist_set",
    "delete_component",
    "find_set_for_route",
    "list_borrowed_components",
    "list_components",
    "list_playlist_sets",
    "list_routes",
    "list_slots",
    "load_component",
    "load_playlist_set",
    "load_run_entries",
    "record_run",
    "update_component",
    "update_set_parts",
]
