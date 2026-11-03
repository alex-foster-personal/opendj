"""Supersedes: duplicate ``upsert_many`` transaction bodies in
``grid_quality_store.py``, ``grid_audio_fit_store.py``, and
``audio_quality_store.py``.

Shared SQLite upsert for sidecar tables.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
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
