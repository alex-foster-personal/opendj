"""Hub-side listing of tracks awaiting content identity (ADR-0068)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from apps.shared.state.ids import normalise_isrc


@dataclass(frozen=True)
class HashPendingPage:
    stable_ids: tuple[str, ...]
    total: int
    next_cursor: str | None


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def count_hash_pending(conn: sqlite3.Connection) -> int:
    """Live inferred-tier tracks with no hash and no normalizable ISRC."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tracks)")}
    audio_predicate = (
        "AND (audio_hash IS NULL OR audio_hash = '')"
        if "audio_hash" in columns else ""
    )
    rows = conn.execute(
        f"""
        SELECT isrc FROM tracks
        WHERE deleted_at IS NULL
          AND stable_id_tier = 'inferred'
          AND (content_hash IS NULL OR content_hash = '')
          {audio_predicate}
        """
    ).fetchall()
    return sum(
        1
        for (isrc,) in rows
        if normalise_isrc(_as_text(isrc)) is None
    )


def list_hash_pending(
    conn: sqlite3.Connection,
    *,
    limit: int = 500,
    cursor: str | None = None,
) -> HashPendingPage:
    """One page of ``stable_id`` values still pending on this hub."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    params: list[object] = []
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tracks)")}
    audio_predicate = (
        " AND (audio_hash IS NULL OR audio_hash = '')"
        if "audio_hash" in columns else ""
    )
    where = (
        "deleted_at IS NULL AND stable_id_tier = 'inferred' "
        "AND (content_hash IS NULL OR content_hash = '')"
        + audio_predicate
    )
    if cursor:
        where += " AND stable_id > ?"
        params.append(cursor)
    total = count_hash_pending(conn)
    rows = conn.execute(
        f"""
        SELECT stable_id, isrc FROM tracks
        WHERE {where}
        ORDER BY stable_id
        LIMIT ?
        """,
        (*params, limit + 1),
    ).fetchall()
    stable_ids: list[str] = []
    for stable_id, isrc in rows:
        if normalise_isrc(_as_text(isrc)) is None:
            stable_ids.append(str(stable_id))
        if len(stable_ids) >= limit:
            break
    next_cursor = stable_ids[-1] if len(stable_ids) == limit else None
    if len(rows) > limit and stable_ids:
        next_cursor = stable_ids[-1]
    return HashPendingPage(
        stable_ids=tuple(stable_ids[:limit]),
        total=total,
        next_cursor=next_cursor,
    )


__all__ = ["HashPendingPage", "count_hash_pending", "list_hash_pending"]
