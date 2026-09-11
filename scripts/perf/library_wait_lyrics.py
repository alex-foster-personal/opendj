"""Lyrics-side remaining work and index drain for PERFBATCH-01. Read-only."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def cache_ids(data_dir: Path) -> frozenset[str]:
    cache = data_dir / "state" / "lyrics-cache"
    if not cache.is_dir():
        return frozenset()
    return frozenset(path.stem for path in cache.glob("*.json"))


def remaining_present(present_ids: frozenset[str], data_dir: Path) -> frozenset[str]:
    return present_ids - cache_ids(data_dir)


def read_index_meta(index_db: Path) -> dict[str, int] | None:
    if not index_db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{index_db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT last_pending, docs_indexed, created_ms, last_committed_ms "
                "FROM lyrics_index_meta WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {
        "last_pending": int(row[0] or 0),
        "docs_indexed": int(row[1] or 0),
        "created_ms": int(row[2] or 0),
        "last_committed_ms": int(row[3] or 0),
    }


def eta_seconds(
    remaining_n: int,
    fetch_per_s: float | None,
    meta: dict[str, int] | None,
) -> tuple[float | None, str]:
    """Fetch-remaining plus index-remaining. None means the rate was unmeasured."""
    last_pending = 0 if meta is None else meta["last_pending"]
    index_note = "index last_pending=0"
    index_s = 0.0
    if last_pending > 0 and meta is not None:
        elapsed_s = (meta["last_committed_ms"] - meta["created_ms"]) / 1000.0
        if elapsed_s <= 0 or meta["docs_indexed"] <= 0:
            return None, f"index last_pending={last_pending}; index rate unmeasured"
        index_s = last_pending / (meta["docs_indexed"] / elapsed_s)
        index_note = (
            f"ETA is fetch-remaining + index-remaining; last_pending={last_pending}"
        )
    extra = f"remaining_present_without_lyrics_cache={remaining_n}; {index_note}"
    if remaining_n and (fetch_per_s is None or fetch_per_s <= 0):
        return None, extra + "; fetch rate unmeasured"
    fetch_s = 0.0 if not remaining_n or fetch_per_s is None else remaining_n / fetch_per_s
    return fetch_s + index_s, extra
