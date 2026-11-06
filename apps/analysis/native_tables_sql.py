"""Native-analysis v1 table DDL (canonical, projection, source default).

Owner sequence spliced into ``apps.analysis.store._ANALYSIS_TABLES_SQL``
(legacy bootstrap) and ``apps.engine_core.store.schema._NATIVE_ANALYSIS_V1``
(consolidated ladder, migration v2). Analysis modules must not import
engine_core.
"""
from __future__ import annotations

NATIVE_ANALYSIS_V1_SQL: tuple[str, ...] = (
    # --- native-analysis v1 (spec section 3, "Record") -------------------
    # The deterministic canonical pointer. Rows in `analysis` never
    # overwrite across producers, so which row a reader gets cannot be a
    # last-writer-wins column; it is recomputed from ALL rows on every
    # upsert by one rule (highest semver, tie -> inapp over backfill, cand
    # never eligible). That is what makes the pointer a function of what
    # was produced rather than of the order it was produced in.
    """
    CREATE TABLE IF NOT EXISTS analysis_canonical (
        stable_id        TEXT NOT NULL,
        lane             TEXT NOT NULL,
        backend          TEXT NOT NULL,
        backend_version  TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        PRIMARY KEY (stable_id, lane)
    )
    """,
    # The read-time projection of own scalars. Every scalar reader goes
    # through effective_fields() (apps/analysis/selection.py), which reads
    # THIS table for a lane on own and track_fields for a lane on rbx.
    # Nothing here is ever written into track_fields, so no own value can
    # enter track_field_history or the sync path.
    """
    CREATE TABLE IF NOT EXISTS analysis_projection (
        stable_id        TEXT NOT NULL,
        field            TEXT NOT NULL,
        -- Deliberately typeless: SQLite gives an untyped column BLOB (none)
        -- affinity, so a REAL bpm stays a REAL and a TEXT camelot stays TEXT.
        -- Declaring it TEXT would coerce 128.0 to '128.0' and make every
        -- smartlist numeric operator a lexical comparison, which is the same
        -- class of silent wrongness as sorting 0.10.0 below 0.9.0.
        value,
        status           TEXT NOT NULL,
        reason           TEXT,
        confidence       REAL,
        backend          TEXT NOT NULL,
        backend_version  TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        PRIMARY KEY (stable_id, field)
    )
    """,
    # Spec section 3: the filters on these fields must not scan records.
    "CREATE INDEX IF NOT EXISTS idx_analysis_projection_field_value "
    "ON analysis_projection(field, value)",
    # The persisted per-lane source default: what a PROMOTION writes, and the
    # only half of the selection surface that survives a relaunch. Declared
    # HERE, with the other analysis-domain tables, rather than provisioned by
    # the endpoint that writes it: a durable configuration table the schema
    # authority does not know about is invisible to adoption, to the drift
    # checks and to the database documentation (Codex P2, PR #1549).
    """
    CREATE TABLE IF NOT EXISTS analysis_source_default (
        lane        TEXT PRIMARY KEY,
        source      TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
)
