"""Chunked bulk-select helpers shared by ``state.db`` readers.

Extracted out of ``apps.webui.server.sqlite_backend`` (pin e0f3a90652a9,
Sat 5 Sep 2026): the chunking pattern (SQLite's ``IN (...)`` variable cap)
was duplicated verbatim across ``get_tracks_bulk`` and the narrower
``get_file_paths_bulk`` it grew alongside. One column-parameterised helper
lets a caller ask for exactly the columns it needs.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence

_SQL_CHUNK: int = 500  # keep IN (...) under SQLite's default 999 variable cap


def bulk_select_by_stable_id(
    conn: sqlite3.Connection,
    table: str,
    columns: str,
    stable_ids: Sequence[str],
) -> list[sqlite3.Row]:
    """``SELECT <columns> FROM <table> WHERE stable_id IN (...)``, chunked.

    Ids are deduped (order-preserving) before chunking; a missing id is
    simply absent from the returned rows, same absence contract every
    caller already relies on.
    """
    ids = list(dict.fromkeys(stable_ids))
    rows: list[sqlite3.Row] = []
    for i in range(0, len(ids), _SQL_CHUNK):
        sub = ids[i : i + _SQL_CHUNK]
        placeholders = ",".join("?" * len(sub))
        rows.extend(conn.execute(
            f"SELECT {columns} FROM {table} WHERE stable_id IN ({placeholders})",
            tuple(sub),
        ))
    return rows
