"""Servable line counts of cached line-synced lyrics, for listing rows (VOCALS-FILTER-01).

The ``lyric_verdict`` table only carries ``n_lines`` once the karaoke pipeline
has aligned words, so a track whose lyrics were only FETCHED (LRCLIB, ASR) had
``lyrics.n_lines`` null on every listing row, and the library "Vocals" filter
saw almost nothing. The listing fills that gap with the counts answered here.

The counts come from the lyric-search index (``lyrics-index.db``,
``lyric_rows.line_count``), not from the cache files: the library index
(LIBM-172) must not open a file per row, and the search index already holds
one row per cached entry, built from the servable lines (ASR hallucinations
dropped, LYRICS-12/13). One SELECT answers a whole page. The index trails the
cache by however far its reconcile job is behind; a track it has not reached
yet simply has no count, which the browser reports as not analyzed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from apps.lyrics.search_index_schema import index_path


def bulk_indexed_line_counts(data_dir: Path, stable_ids: Iterable[str]) -> dict[str, int]:
    """Line counts for the asked ids the lyric-search index holds (others are absent).

    A missing index file means nothing has been indexed yet: an empty answer.
    An unreadable index raises, like every other listing read.
    """
    wanted = set(stable_ids)
    db_path = index_path(data_dir)
    if not wanted or not db_path.is_file():
        return {}
    # Plain connection for the same WAL reason as search_index.search_documents;
    # only SELECT is issued.
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT stable_id, line_count FROM lyric_rows").fetchall()
    finally:
        conn.close()
    return {str(sid): int(count) for sid, count in rows if sid in wanted and int(count) > 0}
