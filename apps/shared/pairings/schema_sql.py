"""Phase 08 pairing and smartlist schema migrations.

This module lives outside :mod:`apps.shared.state.schema` on purpose.
Phase 5 (shared state) and Phase 08 ship in parallel, and touching the
Phase 5-owned ``MIGRATIONS`` list from here creates merge churn. Instead
Phase 08 owns its own DDL and calls :func:`ensure_phase08_tables` from
both the pairings and smartlists repos on construction.

The pairing-capture tables predate their migration ledger in production.
Their migration therefore adopts compatible existing tables, preserving rows,
then records the version. A mismatched object fails explicitly instead of
silently redirecting capture data into an incompatible table or view.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

PAIRING_CAPTURE_SCHEMA_VERSION = 1

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
        snapshot_json  TEXT,
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
        modified_at                  TEXT NOT NULL,
        deleted_at                   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_smartlists_name ON smartlists(name)",
)


def migrate_smartlists_deleted_at(conn: sqlite3.Connection) -> None:
    """Add ``deleted_at`` to an existing smartlists table when missing."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='smartlists'"
    ).fetchone()
    if row is None:
        return
    columns = {
        col[1] for col in conn.execute("PRAGMA table_info(smartlists)")
    }
    if "deleted_at" in columns:
        return
    conn.execute("ALTER TABLE smartlists ADD COLUMN deleted_at TEXT")


def migrate_pairings_snapshot_json(conn: sqlite3.Connection) -> None:
    """Add ``snapshot_json`` to an existing pairings table when missing.

    The Create pairing sheet captures both decks' positions and EQ at the
    moment the DJ saves a pairing. Before this column the webui kept those
    pairings in process memory only, so they vanished on restart.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pairings'"
    ).fetchone()
    if row is None:
        return
    columns = {
        col[1] for col in conn.execute("PRAGMA table_info(pairings)")
    }
    if "snapshot_json" in columns:
        return
    conn.execute("ALTER TABLE pairings ADD COLUMN snapshot_json TEXT")

_PAIRING_CAPTURE_V1: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS pairing_sync_snapshots (
        id                    TEXT PRIMARY KEY,
        stable_a              TEXT NOT NULL,
        stable_b              TEXT NOT NULL,
        master_side           TEXT NOT NULL CHECK (master_side IN ('a', 'b')),
        sync_mode             TEXT NOT NULL CHECK (sync_mode IN ('bar', 'beat')),
        a_tempo_ratio         REAL NOT NULL,
        b_tempo_ratio         REAL NOT NULL,
        a_position_beat_n     INTEGER,
        a_position_phase      REAL,
        a_position_ms         REAL NOT NULL,
        b_position_beat_n     INTEGER,
        b_position_phase      REAL,
        b_position_ms         REAL NOT NULL,
        captured_at           TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pairing_snapshots_a "
    "ON pairing_sync_snapshots(stable_a, captured_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pairing_snapshots_b "
    "ON pairing_sync_snapshots(stable_b, captured_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS pairing_alignments (
        id                    TEXT PRIMARY KEY,
        stable_a              TEXT NOT NULL,
        stable_b              TEXT NOT NULL,
        anchor_a_kind         TEXT NOT NULL CHECK (anchor_a_kind IN ('hotcue', 'ms')),
        anchor_b_kind         TEXT NOT NULL CHECK (anchor_b_kind IN ('hotcue', 'ms')),
        anchor_a_slot         TEXT,
        anchor_b_slot         TEXT,
        anchor_a_ms           REAL NOT NULL,
        anchor_b_ms           REAL NOT NULL,
        label                 TEXT,
        created_at            TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pairing_alignments_a "
    "ON pairing_alignments(stable_a, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pairing_alignments_b "
    "ON pairing_alignments(stable_b, created_at DESC)",
)

_CAPTURE_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "pairing_sync_snapshots": frozenset(
        {
            "id", "stable_a", "stable_b", "master_side", "sync_mode",
            "a_tempo_ratio", "b_tempo_ratio", "a_position_beat_n",
            "a_position_phase", "a_position_ms", "b_position_beat_n",
            "b_position_phase", "b_position_ms", "captured_at",
        }
    ),
    "pairing_alignments": frozenset(
        {
            "id", "stable_a", "stable_b", "anchor_a_kind", "anchor_b_kind",
            "anchor_a_slot", "anchor_b_slot", "anchor_a_ms", "anchor_b_ms",
            "label", "created_at",
        }
    ),
}


def ensure_phase08_tables(conn: sqlite3.Connection) -> None:
    """Create the Phase 08 tables if they don't already exist.

    Idempotent. Wraps everything in a single transaction so partial
    failure rolls back cleanly.
    """
    in_transaction = conn.in_transaction
    if not in_transaction:
        conn.execute("BEGIN")
    try:
        for stmt in _PAIRINGS_DDL:
            conn.execute(stmt)
        for stmt in _SMARTLISTS_DDL:
            conn.execute(stmt)
        migrate_smartlists_deleted_at(conn)
        migrate_pairings_snapshot_json(conn)
        if not in_transaction:
            conn.execute("COMMIT")
    except Exception:
        if not in_transaction:
            conn.execute("ROLLBACK")
        raise


def _assert_capture_table_shapes(conn: sqlite3.Connection) -> None:
    for table_name, required_columns in _CAPTURE_REQUIRED_COLUMNS.items():
        row = conn.execute(
            "SELECT type FROM sqlite_master WHERE name = ?", (table_name,)
        ).fetchone()
        if row is None or row[0] != "table":
            raise RuntimeError(
                f"pairing capture schema requires table {table_name!r}, found {row!r}"
            )
        actual_columns = {
            column[1] for column in conn.execute(f"PRAGMA table_info({table_name})")
        }
        missing_columns = sorted(required_columns - actual_columns)
        if missing_columns:
            raise RuntimeError(
                f"pairing capture table {table_name!r} missing required columns: "
                f"{', '.join(missing_columns)}"
            )


def _assert_existing_capture_table_shapes(conn: sqlite3.Connection) -> None:
    """Fail before index DDL can mask an incompatible live table."""
    for table_name, required_columns in _CAPTURE_REQUIRED_COLUMNS.items():
        row = conn.execute(
            "SELECT type FROM sqlite_master WHERE name = ?", (table_name,)
        ).fetchone()
        if row is None:
            continue
        if row[0] != "table":
            raise RuntimeError(
                f"pairing capture schema requires table {table_name!r}, found {row!r}"
            )
        actual_columns = {
            column[1] for column in conn.execute(f"PRAGMA table_info({table_name})")
        }
        missing_columns = sorted(required_columns - actual_columns)
        if missing_columns:
            raise RuntimeError(
                f"pairing capture table {table_name!r} missing required columns: "
                f"{', '.join(missing_columns)}"
            )


def apply_pairing_capture_migrations(conn: sqlite3.Connection) -> int:
    """Adopt or create pairing-capture tables and return their schema version.

    The migration is safe against live databases that already have the two
    archive-era tables: ``CREATE TABLE IF NOT EXISTS`` leaves their rows and
    shape intact, then the required-column audit proves the repository can use
    them before a version marker is written.
    """
    in_transaction = conn.in_transaction
    if not in_transaction:
        conn.execute("BEGIN")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pairing_capture_schema_meta ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM pairing_capture_schema_meta"
        ).fetchone()
        current = int(row[0]) if row is not None else 0
        if current >= PAIRING_CAPTURE_SCHEMA_VERSION:
            _assert_capture_table_shapes(conn)
            if not in_transaction:
                conn.execute("COMMIT")
            return current
        _assert_existing_capture_table_shapes(conn)
        for statement in _PAIRING_CAPTURE_V1:
            conn.execute(statement)
        _assert_capture_table_shapes(conn)
        conn.execute(
            "INSERT INTO pairing_capture_schema_meta(version, applied_at) VALUES (?, ?)",
            (PAIRING_CAPTURE_SCHEMA_VERSION, datetime.now(UTC).isoformat()),
        )
        if not in_transaction:
            conn.execute("COMMIT")
    except Exception:
        if not in_transaction:
            conn.execute("ROLLBACK")
        raise
    return PAIRING_CAPTURE_SCHEMA_VERSION


__all__ = [
    "PAIRING_CAPTURE_SCHEMA_VERSION",
    "apply_pairing_capture_migrations",
    "ensure_phase08_tables",
    "migrate_smartlists_deleted_at",
]
