"""Migration 19 -> 20: per-table write tokens for the CloudSync digest gate (issue #4396).

``sync_write_tokens`` holds one random token per digested table, and three
triggers per table (INSERT, UPDATE, DELETE) replace that table's token on
every row write, from any connection or process, whether or not the writer
also logged to a changelog. :mod:`apps.sync_hub.digest_gate` reuses a digest
only while every token is the one it saw.

A random token rather than a counter, on purpose: a counter bumped inside a
transaction that later rolls back returns to its old value, and the next
committed write then reuses the rolled-back value for different content. A
fresh ``randomblob(16)`` is never reused, so one token names one content
version of its table.

The table list is hard-coded here (migrations are frozen SQL) and pinned to
``apps.sync_hub.sync_set.FK_ORDER`` by
``tests/cloudsync/test_digest_write_gate.py``; the gate also refuses to cache
when any trigger below is missing or altered.
"""

from __future__ import annotations

WRITE_TOKEN_TABLE: str = "sync_write_tokens"

#: Every table :func:`apps.sync_hub.protocol.sync_digest` hashes.
WRITE_TOKEN_TABLES: tuple[str, ...] = (
    "tracks",
    "lyric_verdict",
    "playlists",
    "track_vendor_ids",
    "track_fields",
    "track_locations",
    "sync_policies",
    "playlist_pins",
    "feedback_pins",
    "playlist_memberships",
)

WRITE_EVENTS: tuple[str, ...] = ("INSERT", "UPDATE", "DELETE")


def trigger_name(table: str, event: str) -> str:
    """The name of ``table``'s write-token trigger for ``event``."""
    return f"{WRITE_TOKEN_TABLE}_{table}_{event.lower()}"


def trigger_sql(table: str, event: str) -> str:
    """The exact DDL, which SQLite keeps verbatim in ``sqlite_master.sql``."""
    return (
        f"CREATE TRIGGER {trigger_name(table, event)} AFTER {event} ON {table} "
        f"BEGIN UPDATE {WRITE_TOKEN_TABLE} SET token = randomblob(16) "
        f"WHERE table_name = '{table}'; END"
    )


EXPECTED_TRIGGERS: dict[str, str] = {
    trigger_name(table, event): trigger_sql(table, event)
    for table in WRITE_TOKEN_TABLES
    for event in WRITE_EVENTS
}

_V20: list[str] = [
    f"""
    CREATE TABLE {WRITE_TOKEN_TABLE} (
        table_name TEXT PRIMARY KEY,
        token      BLOB NOT NULL
    ) WITHOUT ROWID
    """,
    *(
        f"INSERT INTO {WRITE_TOKEN_TABLE}(table_name, token) VALUES ('{table}', randomblob(16))"
        for table in WRITE_TOKEN_TABLES
    ),
    *EXPECTED_TRIGGERS.values(),
]

__all__ = [
    "EXPECTED_TRIGGERS",
    "WRITE_EVENTS",
    "WRITE_TOKEN_TABLE",
    "WRITE_TOKEN_TABLES",
    "_V20",
    "trigger_name",
    "trigger_sql",
]
