"""Pinned playlists, the tree's shortcuts (LIBM-87 pin part, issue #12320).

A pin is a shortcut: the tree draws pinned playlists in their own section
above the ordinary entries, and the playlist keeps its membership and its
place in the tree order. Pins live in their own overlay,
``<data>/state/playlist-tree-pins.json`` holding ``{"pins": [playlist_id, ...]}``
in pin order, apart from ``playlist-tree-order.json`` so pinning never moves a
playlist and a reorder never drops a pin.

Like the order overlay it never decides which playlists exist: a pin whose
playlist is gone is skipped on read, and only a live playlist can be pinned.
"""

from __future__ import annotations

from pathlib import Path

from .playlist_tree_order import load_id_list, write_json_atomic

FILENAME = "playlist-tree-pins.json"


def path_for(data_dir: Path) -> Path:
    """Where this library keeps its pins."""
    return data_dir / "state" / FILENAME


def load_pins(path: Path) -> list[str]:
    """The stored pins in pin order, stale ids included, or ``[]`` when nothing was pinned."""
    return load_id_list(path, "pins", "playlist pins")


def live_pins(pins: list[str], live_ids: set[str]) -> list[str]:
    """The pins whose playlist still exists, in pin order."""
    return [pid for pid in pins if pid in live_ids]


def save_pins(path: Path, pins: list[str]) -> None:
    """Replace the stored pins atomically."""
    write_json_atomic(path, {"pins": list(pins)})
