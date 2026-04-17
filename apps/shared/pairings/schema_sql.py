"""Phase 08 table DDL -- ``pairings`` + ``smartlists``.

This module lives outside :mod:`apps.shared.state.schema` on purpose.
Phase 5 (shared state) and Phase 08 ship in parallel, and touching the
Phase 5-owned ``MIGRATIONS`` list from here creates merge churn. Instead
Phase 08 owns its own DDL and calls :func:`ensure_phase08_tables` from
both the pairings and smartlists repos on construction.

Because we use ``CREATE TABLE IF NOT EXISTS`` the call is idempotent and
safe to run every time. When Phase 5's migration framework is ready to
adopt these tables, the DDL here becomes a migration step there and this
module shrinks to a one-line shim.
"""
from __future__ import annotations

import sqlite3

# Pairing-memory edge graph (CAT-03). Shape matches the open-dj v0
# strawman §4.7 Pairing entity field-for-field so Phase 15 export is a
# plain SELECT.
_PAIRINGS_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS pairings (
        from_stable_id TEXT NOT NULL,
        to_stable_id   TEXT NOT NULL,
        direction      TEXT NOT NULL CHECK
                         (direction IN ('into','out_of','either')),
        source         TEXT NOT NULL CHECK
                         (source    IN ('manual','learned','ai')),
        notes          TEXT,
        confidence     REAL CHECK (confidence IS NULL OR
                                   (confidence BETWEEN 0 AND 1)),
        created_at     TEXT NOT NULL,
        modified_at    TEXT NOT NULL,
        PRIMARY KEY (from_stable_id, to_stable_id, direction)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pairings_from   ON pairings(from_stable_id)",
    "CREATE INDEX IF NOT EXISTS idx_pairings_to     ON pairings(to_stable_id)",
    "CREATE INDEX IF NOT EXISTS idx_pairings_source ON pairings(source)",
)

# Smartlist rules (SMART-01/02). Rule is a JSON AST; materialisation
# state lives on the same row so the evaluator and materialiser share
# a single write surface.
_SMARTLISTS_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS smartlists (
        id                           TEXT PRIMARY KEY,
        name                         TEXT NOT NULL UNIQUE,
        rule                         TEXT NOT NULL,
        rule_schema_version          INTEGER NOT NULL DEFAULT 0,
        referenced_fields            TEXT NOT NULL,
        order_by                     TEXT NOT NULL DEFAULT 'added_date desc',
        last_evaluated_at            TEXT,
        last_materialized_track_ids  TEXT,
        created_at                   TEXT NOT NULL,
        modified_at                  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_smartlists_name ON smartlists(name)",
)


def ensure_phase08_tables(conn: sqlite3.Connection) -> None:
    """Create the Phase 08 tables if they don't already exist.

    Idempotent. Wraps everything in a single transaction so partial
    failure rolls back cleanly.
    """
    conn.execute("BEGIN")
    try:
        for stmt in _PAIRINGS_DDL:
            conn.execute(stmt)
        for stmt in _SMARTLISTS_DDL:
            conn.execute(stmt)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


__all__ = ["ensure_phase08_tables"]
