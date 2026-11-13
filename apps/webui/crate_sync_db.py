"""Read-only state.db queries for crate sync scope selection.

Split out of :mod:`apps.webui.crate_sync` (issue #9687); ``crate_sync``
re-exports every name here.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from apps.webui.soft_deletes import has_soft_deletes

# ----- sqlite -------------------------------------------------------------


def _open_ro(path: Path, label: str) -> sqlite3.Connection:
    if not path.is_file():
        raise RuntimeError(f"{label} missing: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_membership_order_key(conn: sqlite3.Connection) -> bool:
    """Probe the real membership columns for order_key; schema_meta can disagree."""
    return any(row[1] == "order_key" for row in conn.execute("PRAGMA table_info(playlist_memberships)"))


def _playlist_members_sql(conn: sqlite3.Connection) -> str:
    """Pick the membership SELECT from the columns that actually exist."""
    live = has_soft_deletes(conn, "playlist_memberships")
    keyed = _has_membership_order_key(conn)
    members_sql_live_keyed = (
        "SELECT stable_id FROM playlist_memberships "
        "WHERE playlist_id = ? AND deleted_at IS NULL "
        "ORDER BY COALESCE(order_key, printf('%08d', position)), position"
    )
    members_sql_live_position = (
        "SELECT stable_id FROM playlist_memberships WHERE playlist_id = ? AND deleted_at IS NULL ORDER BY position"
    )
    members_sql_keyed = (
        "SELECT stable_id FROM playlist_memberships "
        "WHERE playlist_id = ? "
        "ORDER BY COALESCE(order_key, printf('%08d', position)), position"
    )
    members_sql_position = "SELECT stable_id FROM playlist_memberships WHERE playlist_id = ? ORDER BY position"
    if live and keyed:
        return members_sql_live_keyed
    if live:
        return members_sql_live_position
    if keyed:
        return members_sql_keyed
    return members_sql_position


def _playlist_stable_ids(state: sqlite3.Connection, name: str) -> tuple[str, ...]:
    if has_soft_deletes(state, "playlists"):
        lookup_sql = "SELECT playlist_id, name FROM playlists WHERE name = ? AND deleted_at IS NULL"
    else:
        lookup_sql = "SELECT playlist_id, name FROM playlists WHERE name = ?"
    rows = state.execute(lookup_sql, (name,)).fetchall()
    if not rows:
        if has_soft_deletes(state, "playlists"):
            known_sql = "SELECT DISTINCT name FROM playlists WHERE deleted_at IS NULL ORDER BY name"
        else:
            known_sql = "SELECT DISTINCT name FROM playlists ORDER BY name"
        known = [r[0] for r in state.execute(known_sql)]
        raise RuntimeError(f"unknown playlist {name!r}. Known: {', '.join(known) or '(none)'}")
    ids: list[str] = []
    seen: set[str] = set()
    members_sql = _playlist_members_sql(state)
    for playlist_id, _name in rows:
        for (stable_id,) in state.execute(members_sql, (playlist_id,)):
            if stable_id not in seen:
                seen.add(stable_id)
                ids.append(stable_id)
    if not ids:
        raise RuntimeError(f"playlist {name!r} has no members")
    return tuple(ids)
