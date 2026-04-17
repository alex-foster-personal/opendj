"""SQLite schema for the Phase 7 dedup + tag-unification pipeline.

This is the *fallback* schema: when the Phase 5 shared-state store is
not yet available we create these tables in
``data/dedup/phase7.sqlite``. If Phase 5 later lands the same tables in
``data/state/state.db``, migration is a straight-up INSERT SELECT.

Tables:

* ``fingerprints``        -- created by :mod:`apps.shared.fingerprints`
                             (same schema; this file only references it).
* ``duplicate_clusters``  -- one row per cluster.
* ``track_aliases``       -- one row per alias -> canonical.
* ``tag_provenance``      -- insert-only history of unified-tag writes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# fingerprints table is created by FingerprintCache; we only add the
# dedup-specific tables here so both paths can share a DB file.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS duplicate_clusters (
    cluster_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_stable_id   TEXT NOT NULL,
    canonical_path        TEXT NOT NULL,
    rationale             TEXT,
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
"""


def ensure_schema(db_path: Path) -> sqlite3.Connection:
    """Create (or migrate) the dedup schema; return an open connection.

    Safe to call repeatedly. Uses IF NOT EXISTS guards everywhere.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn
