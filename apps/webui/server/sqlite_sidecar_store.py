"""Supersedes: duplicate ``upsert_many`` transaction bodies in
``grid_quality_store.py``, ``grid_audio_fit_store.py``, and
``audio_quality_store.py``.

Shared SQLite upsert for sidecar tables. ``bind_upsert_many`` is the one-arg
public-entry binder for the two grid stores; ``audio_quality_store`` keeps its
two-arg wrapper around ``upsert_many_sidecar``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import astuple
from pathlib import Path
from typing import Any


def upsert_many_sidecar(
    path: Path,
    schema_sql: str,
    table_name: str,
    columns: Sequence[str],
    row_values: Sequence[Sequence[Any]],
) -> None:
    """Create parent dir if needed, ensure schema, replace rows in one transaction."""
    if not row_values:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(schema_sql)
            conn.executemany(
                f"INSERT OR REPLACE INTO {table_name} ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' * len(columns))})",
                row_values,
            )
    finally:
        conn.close()


def bind_upsert_many(
    store_path_fn: Callable[[], Path],
    schema_sql: str,
    table_name: str,
    columns: Sequence[str],
) -> Callable[[Sequence[Any]], None]:
    """Return a public ``upsert_many(rows)`` bound to one sidecar table.

    ``store_path_fn`` is the store module's ``store_path`` function, invoked at
    write time so ``config.STATE_DB`` is read on the write, not at import.
    """

    def upsert_many(rows: Sequence[Any]) -> None:
        """Replace the stored verdict for each row's track, in one transaction."""
        if not rows:
            return
        upsert_many_sidecar(
            store_path_fn(),
            schema_sql,
            table_name,
            columns,
            [astuple(row) for row in rows],
        )

    return upsert_many
