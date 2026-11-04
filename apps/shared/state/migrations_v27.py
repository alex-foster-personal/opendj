"""Migration 26 -> 27: path_availability.mtime_ns (issue #7584).

Listing QLT identity needs the file mtime the scan already keys file_version
on. One INTEGER column, filled by the same stat that writes materialised_size.
No wire bump: path_availability is machine-local.
"""

from __future__ import annotations

_V27: list[str] = [
    "ALTER TABLE path_availability ADD COLUMN mtime_ns INTEGER",
]

__all__ = ["_V27"]
