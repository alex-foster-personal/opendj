"""Lyric hit collection for hybrid collection search (issue #11146)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from apps.lyrics.fuzzy_match import fuzzy_search_indexed, fuzzy_search_indexed_count
from apps.lyrics.search_index import MAX_SEARCH_LIMIT, index_path, search


def _search_batched(conn: sqlite3.Connection, query: str, *, cap: int) -> tuple[list[str], int]:
    """Collect up to ``cap`` FTS hits in pages no larger than ``MAX_SEARCH_LIMIT``."""
    ids: list[str] = []
    total = 0
    while len(ids) < cap:
        page_limit = min(MAX_SEARCH_LIMIT, cap - len(ids))
        page, total = search(conn, query, limit=page_limit, offset=len(ids))
        ids.extend(page)
        if len(page) < page_limit:
            break
    return ids, total


def collect_lyric_hit_ids(
    data_dir: Path,
    query: str,
    *,
    cap: int,
) -> tuple[frozenset[str], frozenset[str], int]:
    """Return (fts_stable_ids, fuzzy_only_stable_ids, total_matches)."""
    if cap <= 0 or not query.strip():
        return frozenset(), frozenset(), 0
    db_path = index_path(data_dir)
    if not db_path.is_file():
        return frozenset(), frozenset(), 0
    conn = sqlite3.connect(str(db_path))
    try:
        fts_ids, fts_total = _search_batched(conn, query, cap=cap)
        fts_set = set(fts_ids)
        remaining = max(0, cap - len(fts_ids))
        fuzzy_extra = fuzzy_search_indexed(conn, query, exclude=fts_set, limit=remaining)
        fuzzy_only = set(fuzzy_extra) - fts_set
        fuzzy_only_total = fuzzy_search_indexed_count(conn, query, exclude=fts_set)
        total = fts_total + fuzzy_only_total
        return frozenset(fts_set), frozenset(fuzzy_only), total
    finally:
        conn.close()


__all__ = ["collect_lyric_hit_ids"]
