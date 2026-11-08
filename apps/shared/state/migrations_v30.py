"""Migration 29 -> 30: non-destructive virtual track edits (LIBEDIT-9179)."""

from __future__ import annotations

from .migrations_v21 import WRITE_TOKEN_TABLE, trigger_name, trigger_sql
from .migrations_v26 import EXPECTED_TRIGGERS as _V26_TRIGGERS

V30_WRITE_TOKEN_TABLES: tuple[str, ...] = ("track_edit",)

_V30_TRIGGERS: dict[str, str] = {
    trigger_name(table, event): trigger_sql(table, event)
    for table in V30_WRITE_TOKEN_TABLES
    for event in ("INSERT", "UPDATE", "DELETE")
}

EXPECTED_TRIGGERS: dict[str, str] = {**_V26_TRIGGERS, **_V30_TRIGGERS}

_V30: list[str] = [
    """
    CREATE TABLE track_edit (
        id TEXT PRIMARY KEY,
        source_track_id TEXT NOT NULL REFERENCES tracks(stable_id),
        label TEXT NOT NULL,
        in_ms INTEGER NOT NULL,
        out_ms INTEGER NOT NULL,
        fx TEXT NOT NULL DEFAULT '{}',
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        origin_device_id TEXT,
        deleted_at TEXT,
        CHECK (in_ms >= 0 AND in_ms < out_ms)
    )
    """,
    "CREATE INDEX idx_track_edit_source ON track_edit(source_track_id) WHERE deleted_at IS NULL",
    "ALTER TABLE playlist_memberships ADD COLUMN edit_id TEXT REFERENCES track_edit(id)",
    *(
        f"INSERT INTO {WRITE_TOKEN_TABLE}(table_name, token) VALUES ('{table}', randomblob(16))"
        for table in V30_WRITE_TOKEN_TABLES
    ),
    *_V30_TRIGGERS.values(),
]

__all__ = ["EXPECTED_TRIGGERS", "V30_WRITE_TOKEN_TABLES", "_V30"]
