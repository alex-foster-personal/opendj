"""Reuse the first live inferred ``stable_id`` for a file path (LIBM-180).

Tier-3 ids still hash ``path_collision_key(path)|mtime`` when a path is new.
A later import or refresh of the same path must not mint a second id just
because mtime moved. Existing duplicate rows are left in place; this module
only stops a third (or second) id from being created.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from apps.shared.state import ids as state_ids
from apps.shared.state.ingest.path_collisions import path_collision_key


def live_inferred_rows(conn: Any) -> list[tuple[str, str]]:
    """Live inferred ``(stable_id, file_path)`` pairs, oldest first."""
    return [
        (str(stable_id), str(file_path))
        for stable_id, file_path in conn.execute(
            "SELECT stable_id, file_path FROM tracks "
            "WHERE deleted_at IS NULL AND file_path IS NOT NULL "
            "AND file_path != '' AND stable_id_tier = 'inferred' "
            "ORDER BY created_at ASC, stable_id ASC"
        ).fetchall()
    ]


def live_inferred_by_path_key(conn: Any) -> dict[str, str]:
    """Oldest live inferred ``stable_id`` per ``path_collision_key``."""
    by_key: dict[str, str] = {}
    for stable_id, file_path in live_inferred_rows(conn):
        key = path_collision_key(file_path)
        if key not in by_key:
            by_key[key] = stable_id
    return by_key


def resolve_inferred_stable_id(
    abs_path: str,
    mtime: float,
    existing_by_key: Mapping[str, str],
) -> tuple[str, str]:
    """Return the live inferred id for ``abs_path``, or mint a new one."""
    found = existing_by_key.get(path_collision_key(abs_path))
    if found is not None:
        return found, "inferred"
    return state_ids.stable_id(isrc=None, abs_path=abs_path, mtime=mtime)


__all__ = [
    "live_inferred_by_path_key",
    "live_inferred_rows",
    "resolve_inferred_stable_id",
]
