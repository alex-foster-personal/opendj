"""SQLite schema for the Phase 7 dedup + tag-unification pipeline.

This is the *fallback* schema: when the Phase 5 shared-state store is
not yet available we create these tables in
``(private path)``. If Phase 5 later lands the same tables in
``(private path)``, migration is a straight-up INSERT SELECT.

Tables:

* ``fingerprints``        -- created by :mod:`apps.shared.fingerprints`
                             (same schema; this file only references it).
* ``duplicate_clusters``  -- one row per cluster.
* ``track_aliases``       -- one row per alias -> canonical.
* ``tag_provenance``      -- insert-only history of unified-tag writes.
* ``events``              -- StateWriter event log for tag-provenance units.
"""

from __future__ import annotations

import sqlite3 as _sqlite3
from pathlib import Path

# fingerprints table is created by FingerprintCache; we only add the
# dedup-specific tables here so both paths can share a DB file.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS duplicate_clusters (
    cluster_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_stable_id   TEXT NOT NULL,
    canonical_path        TEXT NOT NULL,
    rationale             TEXT,
    flagged_manual_review INTEGER NOT NULL DEFAULT 0,
    basis                 TEXT NOT NULL DEFAULT 'fingerprint',
    created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_clusters_canonical
    ON duplicate_clusters(canonical_stable_id);

CREATE TABLE IF NOT EXISTS track_aliases (
    alias_stable_id      TEXT NOT NULL,
    alias_path           TEXT NOT NULL,
    cluster_id           INTEGER NOT NULL REFERENCES duplicate_clusters(cluster_id)
                           ON DELETE CASCADE,
    canonical_stable_id  TEXT NOT NULL,
    similarity           REAL NOT NULL,
    detected_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (alias_stable_id, cluster_id)
);
CREATE INDEX IF NOT EXISTS idx_aliases_cluster ON track_aliases(cluster_id);
CREATE INDEX IF NOT EXISTS idx_aliases_canonical
    ON track_aliases(canonical_stable_id);

CREATE TABLE IF NOT EXISTS tag_provenance (
    stable_id   TEXT NOT NULL,
    field       TEXT NOT NULL,
    value       TEXT,
    source      TEXT NOT NULL,
    confidence  REAL NOT NULL,
    modified_at TIMESTAMP,
    unified_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (stable_id, field, unified_at)
);
CREATE INDEX IF NOT EXISTS idx_provenance_field
    ON tag_provenance(stable_id, field);

-- Durable event log, same DDL as state.db (apps/shared/state/migrations.py).
-- StateWriter appends one row per tag-provenance unit (apps.tags.apply).
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL,
    stable_id    TEXT,
    payload_json TEXT,
    actor        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_stable_id ON events(stable_id) WHERE stable_id IS NOT NULL;
"""


def ensure_schema(db_path: Path) -> None:
    """Create (or migrate) the dedup schema at ``db_path``.

    Safe to call repeatedly. Uses IF NOT EXISTS guards everywhere. A caller
    outside ``apps.dedup`` opens its own connection afterwards: an open
    connection never crosses the contract, so none is returned or kept here.
    That is why an in-memory database is refused: it lives only as long as the
    connection that migrated it, so the caller's own connection would open a
    second, empty one and fail on its first write.
    """
    if str(db_path) == ":memory:":
        raise ValueError("the dedup contract migrates a database file; an in-memory database cannot outlive it")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        cluster_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(duplicate_clusters)")}
        if "flagged_manual_review" not in cluster_columns:
            conn.execute("ALTER TABLE duplicate_clusters ADD COLUMN flagged_manual_review INTEGER NOT NULL DEFAULT 0")
        if "basis" not in cluster_columns:
            conn.execute("ALTER TABLE duplicate_clusters ADD COLUMN basis TEXT NOT NULL DEFAULT 'fingerprint'")
        conn.commit()
    finally:
        conn.close()
