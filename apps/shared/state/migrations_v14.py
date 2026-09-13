"""Migration 13 -> 14: per-playlist forbid_duplicates flag (LIBM-D2 / #2416).

Adds ``forbid_duplicates`` to ``playlists`` so a container can reject extra
copies of an already-present track while the default remains allow-duplicates.
"""
from __future__ import annotations

_V14: list[str] = [
    "ALTER TABLE playlists ADD COLUMN forbid_duplicates INTEGER NOT NULL DEFAULT 0",
]

__all__ = ["_V14"]
