"""Play-order storage + serialisation (Phase 13, PLAY-01 / PLAY-03).

A playlist can hold >= 1 named play-orders plus a virtual ``"default"``
ordering that mirrors the native ``TrackNo`` sequence. Each named order is
a complete, independent permutation of the same tracks (open-dj v0
`PlayOrder`, docs/open-dj-v0-strawman.md §4.6).

Module layout:

* ``schema``     -- SQL migration (play_orders + play_order_entries tables).
* ``validation`` -- per-track override validators (PLAY-03).
* ``store``      -- CRUD helpers (create / add_entry / load / list / delete).
* ``serde``      -- open-dj JSON round-trip.
* ``events``     -- event-bus helper (best-effort no-op fallback).

The public API is intentionally narrow; everything is built on a raw
``sqlite3.Connection`` so callers own the DB handle + transactions.
"""
from __future__ import annotations

from .schema import SCHEMA_VERSION, apply_play_order_migrations
from .store import (
    DEFAULT_ORDER_NAME,
    PlayOrder,
    PlayOrderEntry,
    add_entry,
    create_play_order,
    delete_play_order,
    list_play_orders,
    load_play_order,
)
from .serde import play_order_from_opendj, play_order_to_opendj
from .validation import (
    validate_key_sync,
    validate_target_key,
    validate_target_tempo,
)

__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_ORDER_NAME",
    "PlayOrder",
    "PlayOrderEntry",
    "apply_play_order_migrations",
    "create_play_order",
    "add_entry",
    "load_play_order",
    "list_play_orders",
    "delete_play_order",
    "play_order_to_opendj",
    "play_order_from_opendj",
    "validate_target_key",
    "validate_target_tempo",
    "validate_key_sync",
]
