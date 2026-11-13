"""SQLite schema and migrations for the title-fix sidecar store (split from title_fix_store)."""

from __future__ import annotations

import sqlite3

from .title_fix import Verdict
from .title_fix_rows import PRODUCER_NOTE_PREFIX
from .title_fix_store_types import CFG, ConfigError

# ----------------------------------------------------------------- store

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    scan_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    state TEXT NOT NULL CHECK (state IN ('running','done','capped','failed')),
    requested INTEGER NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    spend_cap_usd REAL NOT NULL,
    error TEXT,
    auto_batch_id TEXT,
    heartbeat_at TEXT NOT NULL,
    auto_apply_policy TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES scans(scan_id),
    stable_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    original_title TEXT NOT NULL,
    original_artist TEXT NOT NULL,
    original_artists_json TEXT NOT NULL,
    proposed_title TEXT NOT NULL,
    proposed_artist TEXT NOT NULL,
    original_artist_credit TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    fixer_status TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL,
    jev_kind TEXT NOT NULL,
    notes_json TEXT NOT NULL,
    error TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','applied','rejected','superseded','stale','undone','noop')),
    batch_id TEXT,
    tags_added_json TEXT NOT NULL DEFAULT '[]',
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS proposals_state ON proposals(state, fixer_status);
CREATE INDEX IF NOT EXISTS proposals_stable_id ON proposals(stable_id);
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('manual','auto')),
    policy TEXT NOT NULL,
    proposal_count INTEGER NOT NULL,
    undone_at TEXT,
    committed_at TEXT,
    undo_started_at TEXT,
    undo_targets_json TEXT,
    applied_state_json TEXT
);
"""


# Columns added after the first shipped schema. CREATE TABLE IF NOT EXISTS never alters an existing table,
# so a store file written by an older build gets them here. (table, column, DDL, backfill SQL or None)
_ADDED_COLUMNS: tuple[tuple[str, str, str, str | None], ...] = (
    (
        "scans",
        "auto_apply_policy",
        "TEXT NOT NULL DEFAULT ''",
        None,
    ),  # the policy the scan started under (settings can move)
    (
        "scans",
        "heartbeat_at",
        "TEXT NOT NULL DEFAULT ''",
        None,
    ),  # '' sorts before any cutoff: an old running row is stale
    (
        "batches",
        "committed_at",
        "TEXT",
        "UPDATE batches SET committed_at = created_at",
    ),  # old batches were already written
    ("batches", "undo_started_at", "TEXT", None),
    (
        "batches",
        "undo_targets_json",
        "TEXT",
        None,
    ),  # the proposal ids a running undo restores (a partial undo skips some)
    ("batches", "applied_state_json", "TEXT", None),  # proposal_id -> the track ETag and tags as of the write
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, ddl, backfill in _ADDED_COLUMNS:
        if column in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        if backfill is not None:
            conn.execute(backfill)
    _migrate_legacy_etags(conn)


def _migrate_legacy_etags(conn: sqlite3.Connection) -> None:
    """The earlier v2 build recorded only ETags (applied_etags_json). Keep them as a baseline with no tag set, so Undo
    still refuses a row edited since the apply instead of treating the batch as having no baseline."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(batches)")}
    if "applied_etags_json" not in columns:
        return
    conn.execute(
        "UPDATE batches SET applied_state_json = (SELECT json_group_object(key, json_object('etag', value, 'tags', NULL)) "
        "FROM json_each(applied_etags_json)) WHERE applied_etags_json IS NOT NULL AND applied_state_json IS NULL"
    )


#: A scan that died after its auto-apply committed (the scan row is a separate write) keeps its direct link to the
#: undoable batch: the committed auto batch that claimed this scan's proposals.
_RECOVER_AUTO_BATCH = (
    "auto_batch_id = COALESCE(auto_batch_id, (SELECT b.batch_id FROM proposals p JOIN batches b ON b.batch_id = p.batch_id "
    "WHERE p.scan_id = scans.scan_id AND b.kind = 'auto' AND b.committed_at IS NOT NULL ORDER BY b.created_at LIMIT 1))"
)


def _notes_with_producer(verdict: Verdict) -> list[str]:
    """A producer credit the proposal removes from the title has no track field to move to, so it is recorded on the
    proposal itself: the credit is never only in a title the apply is about to rewrite."""
    return [*verdict.notes, *([f"{PRODUCER_NOTE_PREFIX} {verdict.producer}"] if verdict.producer else [])]
