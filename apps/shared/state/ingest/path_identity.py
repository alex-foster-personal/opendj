"""Reuse live ``stable_id`` rows before minting (LIBM-180, LIBM-12770).

Tier-3 ids still hash ``path_collision_key(path)|mtime`` when a path is new.
A later import or refresh of the same path must not mint a second id just
because mtime moved. Rekordbox ingest also reuses a row already linked by
vendor id or, when audio is hashable, by content or audio hash, so a library
path prefix move does not mint a second id. Hash fallback does not claim a row
already linked to a different rekordbox content id that is present in the
current ingest listing.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
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


def live_rekordbox_stable_ids_by_vendor(conn: Any) -> dict[str, tuple[str, str]]:
    """Live rekordbox ``vendor_id`` to ``(stable_id, tier)``, oldest row first."""
    by_vendor: dict[str, tuple[str, str]] = {}
    rows = conn.execute(
        "SELECT t.stable_id, t.stable_id_tier, v.vendor_id FROM tracks t "
        "INNER JOIN track_vendor_ids v ON v.stable_id = t.stable_id "
        "WHERE t.deleted_at IS NULL AND v.vendor = 'rekordbox' "
        "AND v.deleted_at IS NULL "
        "ORDER BY t.created_at ASC, t.stable_id ASC"
    ).fetchall()
    for stable_id, tier, vendor_id in rows:
        key = str(vendor_id)
        if key not in by_vendor:
            by_vendor[key] = (str(stable_id), str(tier))
    return by_vendor


def lookup_live_stable_id_by_hash(
    conn: Any,
    content_hash: str | None,
    audio_hash: str | None,
) -> tuple[str, str] | None:
    """Oldest live track with the same content or audio hash, if any."""
    if content_hash:
        row = conn.execute(
            "SELECT stable_id, stable_id_tier FROM tracks "
            "WHERE deleted_at IS NULL AND content_hash = ? "
            "ORDER BY created_at ASC, stable_id ASC LIMIT 1",
            (content_hash,),
        ).fetchone()
        if row is not None:
            return str(row[0]), str(row[1])
    if audio_hash:
        row = conn.execute(
            "SELECT stable_id, stable_id_tier FROM tracks "
            "WHERE deleted_at IS NULL AND audio_hash = ? "
            "ORDER BY created_at ASC, stable_id ASC LIMIT 1",
            (audio_hash,),
        ).fetchone()
        if row is not None:
            return str(row[0]), str(row[1])
    return None


def _live_rekordbox_vendor_id_for(conn: Any, stable_id: str) -> str | None:
    """Live rekordbox vendor id for ``stable_id``, if linked."""
    row = conn.execute(
        "SELECT vendor_id FROM track_vendor_ids WHERE stable_id = ? AND vendor = 'rekordbox' AND deleted_at IS NULL",
        (stable_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0])


def resolve_rb_existing_stable_id(
    conn: Any,
    rb_vendor_id: str,
    content_hash: str | None,
    audio_hash: str | None,
    *,
    rb_vendor_index: Mapping[str, tuple[str, str]] | None = None,
    present_rb_vendor_ids: Collection[str] | None = None,
) -> tuple[str, str] | None:
    """Reuse a live row for this rekordbox content id or hashed audio."""
    if rb_vendor_index is not None:
        found = rb_vendor_index.get(rb_vendor_id)
    else:
        found = live_rekordbox_stable_ids_by_vendor(conn).get(rb_vendor_id)
    if found is not None:
        return found
    hash_match = lookup_live_stable_id_by_hash(conn, content_hash, audio_hash)
    if hash_match is None:
        return None
    sid, tier = hash_match
    if present_rb_vendor_ids is not None:
        linked = _live_rekordbox_vendor_id_for(conn, sid)
        if linked is not None and linked != rb_vendor_id and linked in present_rb_vendor_ids:
            return None
    return sid, tier


__all__ = [
    "live_inferred_by_path_key",
    "live_inferred_rows",
    "live_rekordbox_stable_ids_by_vendor",
    "lookup_live_stable_id_by_hash",
    "resolve_inferred_stable_id",
    "resolve_rb_existing_stable_id",
]
