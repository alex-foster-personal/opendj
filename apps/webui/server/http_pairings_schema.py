"""DDL and migration entry for PAIR-04 ``http_pairings`` on state.db.

Standard library at import time: ``scripts.sync_drift_subject`` imports
:func:`ensure_http_pairings_table` from here so the quality gate never loads
``pairings_sqlite`` (which pulls the HTTP backend stack).
"""

from __future__ import annotations

import sqlite3

HTTP_PAIRINGS_SCHEMA_SOURCE = "apps/webui/server/http_pairings_schema.py"

_HTTP_PAIRINGS_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS http_pairings (
        pairing_id     TEXT PRIMARY KEY,
        from_stable_id TEXT NOT NULL,
        to_stable_id   TEXT NOT NULL,
        direction      TEXT NOT NULL CHECK (direction IN ('->', '<->')),
        source         TEXT NOT NULL CHECK (source IN ('manual', 'learned', 'ai')),
        notes          TEXT,
        snapshot_json  TEXT,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        graph_owner_stamp TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_http_pairings_from ON http_pairings(from_stable_id)",
    "CREATE INDEX IF NOT EXISTS idx_http_pairings_to ON http_pairings(to_stable_id)",
    "CREATE INDEX IF NOT EXISTS idx_http_pairings_source ON http_pairings(source)",
)


def ensure_http_pairings_table(conn: sqlite3.Connection) -> None:
    for stmt in _HTTP_PAIRINGS_DDL:
        conn.execute(stmt)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(http_pairings)")}
    if "graph_owner_stamp" not in columns:
        conn.execute("ALTER TABLE http_pairings ADD COLUMN graph_owner_stamp TEXT")


__all__ = ["HTTP_PAIRINGS_SCHEMA_SOURCE", "ensure_http_pairings_table"]
