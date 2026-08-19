"""The ONE authoritative SQLite schema for the Open DJ store.

Today ``state.db`` is bootstrapped from ~17 places: only
``apps/shared/state/schema.py`` participates in a versioned migration
ladder, and everything else runs ad-hoc ``CREATE TABLE IF NOT EXISTS``
on first use from whichever module happens to own the domain. This
module is the consolidation target: every table and index the engine
owns, declared once, in one migration ladder, with a comment naming the
legacy file each definition came from.

This is CONSOLIDATION, NOT REDESIGN. Where a legacy definition is odd
(a missing index, a CHECK that drifted, a FK that one bootstrap declares
and another does not) the odd shape is reproduced verbatim here and the
oddity is recorded in ``REPORT.md`` next to this file. Changing shapes
is a separate, later decision.

Versioning
----------
The consolidated ladder reuses the legacy ``schema_meta`` table so a
single counter governs the file, but stamps its versions at an offset
(:data:`VERSION_OFFSET`) so it cannot collide with the legacy
shared-state counter (1..5) that may already be present. Rows below the
offset are legacy shared-state versions; rows at or above it are
consolidated versions.

Two entry paths, both through :func:`apply_migrations`:

* **fresh DB** -- no consolidated marker, no pre-existing engine tables:
  ``MIGRATIONS[0]`` runs and creates the whole consolidated schema.
* **existing DB** -- adoption. The runner verifies which of the expected
  objects already exist, creates only the missing ones, replays the
  legacy backfills that are data-bearing (idempotent), and records the
  adoption as its own :data:`ADOPTION_VERSION` row in ``schema_meta``.
  Adoption refuses (loudly) to run against a DB below
  :data:`MIN_ADOPTABLE_LEGACY_VERSION`, because those legacy steps are
  table REBUILDS (PK change, widened CHECK) rather than pure creates and
  must be applied by the legacy runner first.

Scope note: tables that legacy code creates inside a *vendor* database
(rekordbox ``master.db``) are declared here too, as
:data:`VENDOR_SIDECAR_DDL`, so the DDL has a single home -- but they are
deliberately NOT part of the state-DB ladder. See ``REPORT.md``.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

# --- version counters -----------------------------------------------------

SCHEMA_VERSION: int = 1
"""Target version of the consolidated ladder (index into :data:`MIGRATIONS`)."""

VERSION_OFFSET: int = 1000
"""Consolidated versions are stamped into ``schema_meta`` at this offset so
they never collide with the legacy shared-state counter (1..5)."""

ADOPTION_VERSION: int = VERSION_OFFSET
"""Marker row written when the runner ADOPTED a pre-existing DB rather than
creating it from scratch. Distinct from the ``VERSION_OFFSET + n`` rows so a
reader can tell "this file predates consolidation" from "this file was born
consolidated"."""

LEGACY_SHARED_STATE_VERSION: int = 5
"""Terminal version of ``apps/shared/state/schema.py``'s own ladder."""

MIN_ADOPTABLE_LEGACY_VERSION: int = 3
"""Below this the legacy ladder still owes table REBUILDS -- v1->v2 rewrites
``track_field_history``'s primary key and v2->v3 widens the ``track_fields``
source CHECK to include ``'webui'``. Adoption only creates missing objects,
so it cannot repair an already-wrong shape; it raises instead."""


# ==========================================================================
# DOMAIN: identity / provenance / playlists / event log
# Legacy source: apps/shared/state/schema.py  (the only versioned bootstrap)
# Shapes below are the END STATE of that ladder at v5, i.e. track_fields
# carries the v3 widened CHECK and track_field_history carries the v2
# surrogate AUTOINCREMENT key, not their v1 text.
# ==========================================================================

_STATE_CORE: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tracks (
        stable_id       TEXT PRIMARY KEY,
        stable_id_tier  TEXT NOT NULL CHECK
                          (stable_id_tier IN ('isrc','fingerprint','inferred')),
        title           TEXT,
        artists_json    TEXT,
        album           TEXT,
        isrc            TEXT,
        duration_ms     INTEGER,
        file_path       TEXT,
        content_hash    TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tracks_isrc ON tracks(isrc) WHERE isrc IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_tracks_file_path ON tracks(file_path)",
    """
    CREATE TABLE IF NOT EXISTS track_vendor_ids (
        stable_id  TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        vendor     TEXT NOT NULL,
        vendor_id  TEXT NOT NULL,
        PRIMARY KEY (stable_id, vendor)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_track_vendor_ids_vendor "
    "ON track_vendor_ids(vendor, vendor_id)",
    # Legacy shape is the v3 rebuild (``track_fields_v3`` renamed), so the
    # source enum includes 'webui'. Mirror of apps.shared.state.types.SOURCES.
    """
    CREATE TABLE IF NOT EXISTS track_fields (
        stable_id    TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        field_name   TEXT NOT NULL,
        value_json   TEXT NOT NULL,
        source       TEXT NOT NULL CHECK (source IN
                       ('mik','rekordbox','djay','serato','traktor',
                        'open-dj-tool','manual','inferred','webui')),
        confidence   REAL CHECK (confidence IS NULL OR
                                 (confidence >= 0 AND confidence <= 1)),
        modified_at  TEXT NOT NULL,
        PRIMARY KEY (stable_id, field_name)
    )
    """,
    # Legacy shape is the v2 rebuild: surrogate key so history is truly
    # append-only even when two rewrites land in the same clock tick.
    """
    CREATE TABLE IF NOT EXISTS track_field_history (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        stable_id     TEXT NOT NULL,
        field_name    TEXT NOT NULL,
        value_json    TEXT NOT NULL,
        source        TEXT NOT NULL,
        confidence    REAL,
        modified_at   TEXT NOT NULL,
        superseded_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_track_field_history_lookup "
    "ON track_field_history(stable_id, field_name, superseded_at)",
    """
    CREATE TABLE IF NOT EXISTS playlists (
        playlist_id   TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        vendor        TEXT NOT NULL,
        vendor_pl_id  TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        UNIQUE (vendor, vendor_pl_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playlist_memberships (
        playlist_id  TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
        stable_id    TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        position     INTEGER NOT NULL,
        PRIMARY KEY (playlist_id, position)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS adapters (
        adapter_id   TEXT PRIMARY KEY,
        last_run_at  TEXT,
        last_ok      INTEGER NOT NULL DEFAULT 0,
        notes        TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT NOT NULL,
        kind         TEXT NOT NULL,
        stable_id    TEXT,
        payload_json TEXT,
        actor        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_stable_id ON events(stable_id) "
    "WHERE stable_id IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS track_locations (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        stable_id     TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        kind          TEXT NOT NULL CHECK (kind IN ('local', 'remote')),
        role          TEXT NOT NULL DEFAULT 'alternate'
                        CHECK (role IN ('primary', 'alternate')),
        file_path     TEXT,
        remote_url    TEXT,
        venue_key     TEXT,
        venue_rank    INTEGER,
        available     INTEGER NOT NULL DEFAULT 0,
        probed_at     TEXT,
        content_hash  TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        CHECK (
            (file_path IS NOT NULL AND file_path != '')
            OR (remote_url IS NOT NULL AND remote_url != '')
        )
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_track_locations_stable "
    "ON track_locations(stable_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_track_locations_path "
    "ON track_locations(stable_id, kind, file_path) "
    "WHERE file_path IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_track_locations_url "
    "ON track_locations(stable_id, kind, remote_url) "
    "WHERE remote_url IS NOT NULL",
)


# ==========================================================================
# DOMAIN: analysis records + analysis event log
# Legacy source: apps/analysis/store.py  (_ANALYSIS_TABLES_SQL, run by
# open_conn() on top of the shared-state migrations)
# ==========================================================================

_ANALYSIS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS analysis (
        stable_id        TEXT NOT NULL,
        backend          TEXT NOT NULL,
        backend_version  TEXT NOT NULL,
        analyzed_at      TEXT NOT NULL,
        duration_s       REAL NOT NULL,
        sample_rate      INTEGER NOT NULL,
        bpm              REAL NOT NULL,
        bpm_confidence   REAL NOT NULL,
        key_camelot      TEXT NOT NULL,
        key_openkey      TEXT NOT NULL,
        key_confidence   REAL NOT NULL,
        energy           INTEGER NOT NULL,
        energy_source    TEXT NOT NULL,
        record_json      TEXT NOT NULL,
        PRIMARY KEY (stable_id, backend, backend_version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_analysis_stable_id ON analysis(stable_id)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_backend   ON analysis(backend)",
    """
    CREATE TABLE IF NOT EXISTS analysis_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT NOT NULL,
        event_type   TEXT NOT NULL,
        stable_id    TEXT,
        payload_json TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_analysis_events_type   ON analysis_events(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_events_stable ON analysis_events(stable_id)",
)


# ==========================================================================
# DOMAIN: curation -- pairing-memory edges + smartlist rules
# Legacy source: apps/shared/pairings/schema_sql.py (ensure_phase08_tables,
# called from the pairings repo AND the smartlists repo on construction)
# ==========================================================================

_CURATION: tuple[str, ...] = (
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


# ==========================================================================
# DOMAIN: play orders (generated set orderings)
# Legacy source: apps/shared/play_orders/schema.py (its own private
# ``play_orders_schema_meta`` counter, deliberately not schema_meta)
# ==========================================================================

_PLAY_ORDERS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS play_orders (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        playlist_id     TEXT NOT NULL,
        name            TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        generated_by    TEXT,
        goal_json       TEXT,
        schema_version  INTEGER NOT NULL DEFAULT 1,
        UNIQUE(playlist_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS play_order_entries (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        play_order_id   INTEGER NOT NULL REFERENCES play_orders(id) ON DELETE CASCADE,
        stable_id       TEXT NOT NULL,
        position        INTEGER NOT NULL,
        target_key      TEXT,
        target_tempo    REAL,
        key_sync        INTEGER,
        transition_hint TEXT,
        UNIQUE(play_order_id, position)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_play_order_entries_stable_id "
    "ON play_order_entries(stable_id)",
    "CREATE INDEX IF NOT EXISTS idx_play_orders_playlist "
    "ON play_orders(playlist_id)",
    # Kept, not dropped: legacy callers still run apply_play_order_migrations
    # on every process start. Creating the counter here (and stamping it, see
    # _stamp_legacy_counters) makes that call a no-op instead of a second,
    # racing bootstrap.
    """
    CREATE TABLE IF NOT EXISTS play_orders_schema_meta (
        version    INTEGER PRIMARY KEY,
        applied_at TEXT    NOT NULL
    )
    """,
)


# ==========================================================================
# DOMAIN: spotify import (snapshot idempotence + acquisition queue)
# Legacy source: apps/spotify/state_writer.py (AUX_MIGRATIONS /
# ensure_aux_tables, run on every open of the state DB)
# ==========================================================================

_SPOTIFY: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS spotify_playlist_meta (
        playlist_id      TEXT PRIMARY KEY REFERENCES playlists(playlist_id)
                           ON DELETE CASCADE,
        vendor_pl_id     TEXT NOT NULL,
        snapshot_id      TEXT NOT NULL,
        track_count      INTEGER NOT NULL,
        matched_count    INTEGER NOT NULL,
        pending_count    INTEGER NOT NULL,
        last_import_at   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pending_tracks (
        pending_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        playlist_id           TEXT NOT NULL REFERENCES playlists(playlist_id)
                                ON DELETE CASCADE,
        position              INTEGER NOT NULL,
        spotify_uri           TEXT NOT NULL,
        isrc                  TEXT,
        title                 TEXT NOT NULL,
        artist                TEXT NOT NULL,
        album                 TEXT,
        duration_ms           INTEGER,
        suggested_sources_json TEXT NOT NULL,
        status                TEXT NOT NULL DEFAULT 'pending'
                                CHECK (status IN
                                 ('pending','purchased','resolved','abandoned')),
        added_at              TEXT NOT NULL,
        resolved_stable_id    TEXT REFERENCES tracks(stable_id) ON DELETE SET NULL,
        resolved_at           TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pending_tracks_playlist "
    "ON pending_tracks(playlist_id)",
    "CREATE INDEX IF NOT EXISTS idx_pending_tracks_isrc "
    "ON pending_tracks(isrc) WHERE isrc IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_pending_tracks_status "
    "ON pending_tracks(status)",
)


# ==========================================================================
# DOMAIN: recorded sets (session + timeline)
# Legacy source: apps/sets/state.py -- _PHASE5_SCHEMA_SQL, the shared-DB
# variant. Its standalone variant names the timeline table ``events``, which
# collides head-on with the shared-state event log; consolidation therefore
# takes the ``set_events`` naming. See REPORT.md.
# ==========================================================================

_SETS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sets (
        session_id     TEXT PRIMARY KEY,
        started_at     TEXT NOT NULL,
        ended_at       TEXT,
        capture_device TEXT NOT NULL,
        share_state    TEXT NOT NULL DEFAULT 'private'
                          CHECK (share_state IN ('private','shared_local','shared_cloud')),
        notes          TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS set_events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id      TEXT NOT NULL REFERENCES sets(session_id) ON DELETE CASCADE,
        timestamp_s     REAL NOT NULL,
        wall_clock      TEXT NOT NULL,
        deck            TEXT,
        track_stable_id TEXT,
        action          TEXT NOT NULL,
        value_json      TEXT,
        source          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_set_events_session "
    "ON set_events(session_id, timestamp_s)",
)


# ==========================================================================
# DOMAIN: daemon settings (key/value)
# Legacy source: apps/voice/settings.py (_SCHEMA, its own
# data/voice/settings.sqlite file)
# ==========================================================================

_SETTINGS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)


# ==========================================================================
# DOMAIN: dedup + tag unification
# Legacy source: apps/dedup/schema.py (SCHEMA_SQL / ensure_schema, its own
# data/dedup/phase7.sqlite fallback file)
# ==========================================================================

_DEDUP: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS duplicate_clusters (
        cluster_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_stable_id   TEXT NOT NULL,
        canonical_path        TEXT NOT NULL,
        rationale             TEXT,
        flagged_manual_review INTEGER NOT NULL DEFAULT 0,
        created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clusters_canonical "
    "ON duplicate_clusters(canonical_stable_id)",
    """
    CREATE TABLE IF NOT EXISTS track_aliases (
        alias_stable_id      TEXT NOT NULL,
        alias_path           TEXT NOT NULL,
        cluster_id           INTEGER NOT NULL REFERENCES duplicate_clusters(cluster_id)
                               ON DELETE CASCADE,
        canonical_stable_id  TEXT NOT NULL,
        similarity           REAL NOT NULL,
        detected_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (alias_stable_id, cluster_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_aliases_cluster ON track_aliases(cluster_id)",
    "CREATE INDEX IF NOT EXISTS idx_aliases_canonical "
    "ON track_aliases(canonical_stable_id)",
    """
    CREATE TABLE IF NOT EXISTS tag_provenance (
        stable_id   TEXT NOT NULL,
        field       TEXT NOT NULL,
        value       TEXT,
        source      TEXT NOT NULL,
        confidence  REAL NOT NULL,
        modified_at TIMESTAMP,
        unified_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (stable_id, field, unified_at)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_provenance_field "
    "ON tag_provenance(stable_id, field)",
)


# ==========================================================================
# DOMAIN: content caches (audio fingerprints, file digests)
# Legacy sources:
#   fingerprints -- apps/shared/fingerprints.py (_CACHE_SCHEMA). A SECOND,
#     divergent ``fingerprints`` definition lives in apps/sync/fingerprint.py;
#     this one wins because apps/dedup/schema.py documents it as the shared
#     shape. See REPORT.md.
#   file_hashes  -- apps/shared/hashing.py (HashCache._ensure_schema). Legacy
#     drops+recreates on a PRAGMA user_version mismatch instead of migrating.
# ==========================================================================

_CACHES: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS fingerprints (
        path         TEXT PRIMARY KEY,
        stable_id    TEXT,
        fingerprint  TEXT NOT NULL,
        duration     REAL NOT NULL,
        size         INTEGER NOT NULL,
        mtime        REAL NOT NULL,
        bitrate      INTEGER,
        computed_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fingerprints_stable_id "
    "ON fingerprints(stable_id)",
    """
    CREATE TABLE IF NOT EXISTS file_hashes (
        path      TEXT NOT NULL PRIMARY KEY,
        size      INTEGER NOT NULL,
        mtime_ns  INTEGER NOT NULL,
        digest    TEXT NOT NULL
    )
    """,
)


# ==========================================================================
# DOMAIN: launcher palette (FTS + frecency)
# Legacy source: apps/launcher/scripts/bootstrap_db.py --
# apply_launcher_migration(), the state.db path. NOTE this is NOT the same
# tracks_frecency as that module's standalone SCHEMA constant (which adds a
# REFERENCES tracks(stable_id) FK). See REPORT.md.
# ==========================================================================

_LAUNCHER: tuple[str, ...] = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
        title, artist, album, genre, key, tags,
        tokenize='unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tracks_frecency (
        stable_id        TEXT PRIMARY KEY,
        plays            INTEGER DEFAULT 0,
        drags            INTEGER DEFAULT 0,
        last_played_at   INTEGER,
        last_dragged_at  INTEGER
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_frecency_drags
        ON tracks_frecency(drags DESC, last_dragged_at DESC)
    """,
)


# --- domain registry ------------------------------------------------------

DOMAINS: dict[str, tuple[str, ...]] = {
    "state_core": _STATE_CORE,
    "analysis": _ANALYSIS,
    "curation": _CURATION,
    "play_orders": _PLAY_ORDERS,
    "spotify": _SPOTIFY,
    "sets": _SETS,
    "settings": _SETTINGS,
    "dedup": _DEDUP,
    "caches": _CACHES,
    "launcher": _LAUNCHER,
}
"""Every consolidated domain -> its DDL statements, in creation order."""

LEGACY_SOURCES: dict[str, str] = {
    "state_core": "apps/shared/state/schema.py",
    "analysis": "apps/analysis/store.py",
    "curation": "apps/shared/pairings/schema_sql.py",
    "play_orders": "apps/shared/play_orders/schema.py",
    "spotify": "apps/spotify/state_writer.py",
    "sets": "apps/sets/state.py",
    "settings": "apps/voice/settings.py",
    "dedup": "apps/dedup/schema.py",
    "caches": "apps/shared/fingerprints.py + apps/shared/hashing.py",
    "launcher": "apps/launcher/scripts/bootstrap_db.py",
}
"""Domain -> the legacy file its DDL was lifted from, verbatim."""

TABLES: dict[str, tuple[str, ...]] = {
    "state_core": (
        "tracks",
        "track_vendor_ids",
        "track_fields",
        "track_field_history",
        "playlists",
        "playlist_memberships",
        "adapters",
        "events",
        "track_locations",
    ),
    "analysis": ("analysis", "analysis_events"),
    "curation": ("pairings", "smartlists"),
    "play_orders": ("play_orders", "play_order_entries", "play_orders_schema_meta"),
    "spotify": ("spotify_playlist_meta", "pending_tracks"),
    "sets": ("sets", "set_events"),
    "settings": ("settings",),
    "dedup": ("duplicate_clusters", "track_aliases", "tag_provenance"),
    "caches": ("fingerprints", "file_hashes"),
    "launcher": ("tracks_fts", "tracks_frecency"),
}
"""Domain -> the tables it owns. ``schema_meta`` is excluded on purpose: it is
migration infrastructure, created by the runner, not domain data."""

ALL_TABLES: tuple[str, ...] = tuple(
    name for names in TABLES.values() for name in names
)


# --- migration ladder -----------------------------------------------------

_V1: list[str] = [stmt for domain in DOMAINS.values() for stmt in domain]
"""Consolidated 0 -> 1: create everything. Fresh-DB path."""

MIGRATIONS: list[list[str]] = [_V1]

# Data-bearing statements the legacy ladder carried alongside its DDL. They
# are re-run on adoption because a DB adopted mid-ladder may have the table
# but not the rows. Idempotent by construction (INSERT OR IGNORE), so they
# are safe on a fully-migrated DB and no-ops on a fresh one.
# Source: apps/shared/state/schema.py _V4.
_ADOPTION_BACKFILL: tuple[str, ...] = (
    """
    INSERT OR IGNORE INTO track_locations(
        stable_id, kind, role, file_path, created_at, updated_at
    )
    SELECT stable_id, 'local', 'primary', file_path, updated_at, updated_at
    FROM tracks
    WHERE file_path IS NOT NULL AND file_path != ''
    """,
)


# ==========================================================================
# VENDOR SIDECAR -- declared here so the DDL has one home, but NOT part of
# the state-DB ladder: legacy creates these inside the rekordbox vendor DB
# (master.db), not state.db.
# Legacy source: apps/webui/server/rb_vendor.py (_ensure_reversal_tables)
# ==========================================================================

VENDOR_SIDECAR_DDL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS rb_hot_cue_reversal ("
    "ID TEXT PRIMARY KEY, ContentID TEXT NOT NULL, Kind INTEGER NOT NULL, "
    "PreimageJson TEXT, PostRevision TEXT NOT NULL, CreatedAt TEXT NOT NULL, "
    "ConsumedAt TEXT)",
    "CREATE TABLE IF NOT EXISTS rb_hot_cue_slot_revision ("
    "ContentID TEXT NOT NULL, Kind INTEGER NOT NULL, Generation INTEGER NOT NULL, "
    "PRIMARY KEY (ContentID, Kind))",
)

VENDOR_SIDECAR_TABLES: tuple[str, ...] = (
    "rb_hot_cue_reversal",
    "rb_hot_cue_slot_revision",
)


def ensure_vendor_sidecar_tables(conn: sqlite3.Connection) -> None:
    """Create the rekordbox-side hot-cue reversal tables. Idempotent.

    ``conn`` must be the VENDOR database (rekordbox ``master.db``), never the
    engine state DB -- these rows are keyed by rekordbox ContentID and only
    mean anything next to the vendor tables they reverse.
    """
    for stmt in VENDOR_SIDECAR_DDL:
        conn.execute(stmt)


# --- runner ---------------------------------------------------------------


class SchemaAdoptionError(RuntimeError):
    """A pre-existing DB cannot be adopted without a legacy migration first."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ensure_meta(conn: sqlite3.Connection) -> None:
    """Create ``schema_meta`` with the legacy shape, byte-for-byte.

    Same DDL as apps/shared/state/schema.py._ensure_meta, so a DB that has
    already been through the legacy runner sees an exact no-op.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT    NOT NULL
        )
        """
    )


def _legacy_version(conn: sqlite3.Connection) -> int:
    """Highest legacy shared-state version stamped in ``schema_meta`` (0 = none)."""
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_meta WHERE version < ?",
        (VERSION_OFFSET,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def consolidated_version(conn: sqlite3.Connection) -> int:
    """Consolidated ladder version recorded in ``schema_meta`` (0 = none).

    Reads the offset rows only, so the legacy 1..5 counter is invisible here.
    The :data:`ADOPTION_VERSION` marker row is exactly ``VERSION_OFFSET`` and
    therefore reads back as version 0 -- adoption records that the file was
    adopted, it does not by itself advance the ladder.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_meta WHERE version >= ?",
        (VERSION_OFFSET,),
    ).fetchone()
    stamped = int(row[0]) if row is not None else 0
    return max(stamped - VERSION_OFFSET, 0)


def existing_objects(conn: sqlite3.Connection) -> set[str]:
    """Names of every table (incl. virtual) already in ``conn``."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(r[0]) for r in rows}


def missing_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Consolidated tables that ``conn`` does not have yet, in declared order."""
    present = existing_objects(conn)
    return tuple(name for name in ALL_TABLES if name not in present)


def _table_sql(conn: sqlite3.Connection, name: str) -> str | None:
    """The live ``CREATE TABLE`` text for ``name``, or None if absent."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _track_fields_accepts_webui(conn: sqlite3.Connection) -> bool:
    """True iff the live ``track_fields`` CHECK already lists ``'webui'``.

    The legacy v2->v3 step widened that enum by REBUILDING the table. Adoption
    only creates missing objects, so a DB stuck on the narrow enum must go
    through the legacy runner first -- this is the probe that catches it.
    """
    sql = _table_sql(conn, "track_fields")
    if sql is None:
        return False
    return "'webui'" in sql


def _track_field_history_has_surrogate_id(conn: sqlite3.Connection) -> bool:
    """True iff the live ``track_field_history`` carries the v2 surrogate key.

    The legacy v1->v2 step REBUILT this table, swapping the natural PK
    ``(stable_id, field_name, superseded_at)`` for an ``id INTEGER PRIMARY KEY
    AUTOINCREMENT`` so history stays append-only when two rewrites land in the
    same clock tick. Same repair class as the ``track_fields`` CHECK widening,
    so it gets the same probe treatment.
    """
    if _table_sql(conn, "track_field_history") is None:
        return False
    columns = conn.execute("PRAGMA table_info(track_field_history)").fetchall()
    return any(str(col[1]) == "id" and int(col[5]) == 1 for col in columns)


_REBUILD_PROBES: tuple[tuple[str, Callable[[sqlite3.Connection], bool], str], ...] = (
    (
        "track_fields",
        _track_fields_accepts_webui,
        "existing track_fields table predates the widened source CHECK "
        "(no 'webui' in the source enum)",
    ),
    (
        "track_field_history",
        _track_field_history_has_surrogate_id,
        "existing track_field_history table predates the v2 rebuild "
        "(no surrogate id INTEGER PRIMARY KEY)",
    ),
)
"""Tables the legacy ladder repairs by REBUILDING, paired with a probe that
reads the LIVE shape. Adoption only creates missing objects, so it cannot fix
any of these; the probes are what make it refuse instead of stamping over the
damage."""


def _assert_adoptable(conn: sqlite3.Connection) -> None:
    """Fail loudly if this DB carries a legacy shape adoption cannot repair.

    The ``schema_meta`` counter is ADVISORY here, not authoritative. It reads 0
    for a brand-new file and equally for a v1-era file whose counter was never
    written (or was truncated), so a floor test alone lets the second case
    through -- and once through, the runner stamps 1..5, the legacy runner
    short-circuits forever, and the rebuild can never happen. The shape probes
    therefore run against every table that is present, whatever the counter
    says; the floor check is kept only as the earlier, better-worded refusal
    for DBs that do carry an honest sub-floor stamp.
    """
    legacy = _legacy_version(conn)
    if 0 < legacy < MIN_ADOPTABLE_LEGACY_VERSION:
        raise SchemaAdoptionError(
            f"state DB is at legacy shared-state schema v{legacy}; adoption "
            f"needs v{MIN_ADOPTABLE_LEGACY_VERSION} or later because v1->v2 "
            "and v2->v3 REBUILD track_field_history / track_fields and this "
            "runner only creates missing objects. Run "
            "apps.shared.state.schema.apply_migrations(conn) first."
        )

    present = existing_objects(conn)
    for table, probe, complaint in _REBUILD_PROBES:
        if table in present and not probe(conn):
            raise SchemaAdoptionError(
                f"{complaint}. The legacy ladder repairs this by REBUILDING "
                f"{table}, and this runner only creates missing objects, so "
                "adoption would stamp the legacy counter to "
                f"v{LEGACY_SHARED_STATE_VERSION} over a shape it cannot fix -- "
                "after which the legacy runner short-circuits and the rebuild "
                "can never run. Run apps.shared.state.schema.apply_migrations"
                "(conn) first, then re-run adoption. (The schema_meta legacy "
                f"counter reads v{legacy}; it is not trusted here because a "
                "missing or truncated counter reads the same as a fresh file.)"
            )


def _stamp_legacy_counters(conn: sqlite3.Connection) -> None:
    """Mark the legacy counters as satisfied so their runners become no-ops.

    Without this, ``apps.shared.state.schema.apply_migrations`` and
    ``apps.shared.play_orders.schema.apply_play_order_migrations`` would each
    re-run their whole ladder against a DB this module already built --
    harmless (every statement is IF NOT EXISTS) but a second, competing
    bootstrap is exactly what consolidation exists to remove.
    """
    now = _now()
    for version in range(1, LEGACY_SHARED_STATE_VERSION + 1):
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(version, applied_at) VALUES (?, ?)",
            (version, now),
        )
    conn.execute(
        "INSERT OR IGNORE INTO play_orders_schema_meta(version, applied_at) "
        "VALUES (1, ?)",
        (now,),
    )


def _record(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(version, applied_at) VALUES (?, ?)",
        (version, _now()),
    )


def _create_all(conn: sqlite3.Connection, statements: list[str]) -> None:
    for stmt in statements:
        conn.execute(stmt)


def _adopt(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Bring a pre-existing DB up to the consolidated shape.

    Creates only what is missing (every statement is already IF NOT EXISTS,
    so this is belt-and-braces: the returned tuple is the audit trail of what
    the file was actually short of), replays the data-bearing backfills, and
    leaves the ADOPTION marker to the caller. Returns the tables that were
    missing before the call.
    """
    _assert_adoptable(conn)
    was_missing = missing_tables(conn)
    _create_all(conn, _V1)
    for stmt in _ADOPTION_BACKFILL:
        conn.execute(stmt)
    return was_missing


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Bring ``conn`` to :data:`SCHEMA_VERSION`; return the resulting version.

    Idempotent. Picks its own path:

    * fresh DB (no engine tables, no schema_meta rows) -> create everything.
    * existing DB -> adopt: verify, create only the missing objects, replay
      idempotent backfills, and stamp :data:`ADOPTION_VERSION` so the file
      records that it predates consolidation.

    Raises :class:`SchemaAdoptionError` when the existing DB carries a legacy
    shape that adoption cannot repair (see
    :data:`MIN_ADOPTABLE_LEGACY_VERSION`).
    """
    _ensure_meta(conn)
    current = consolidated_version(conn)
    if current >= SCHEMA_VERSION:
        return current

    adopting = bool(_legacy_version(conn)) or bool(
        existing_objects(conn) & set(ALL_TABLES)
    )

    conn.execute("BEGIN")
    try:
        if adopting:
            _adopt(conn)
            _record(conn, ADOPTION_VERSION)
        else:
            _create_all(conn, _V1)
        _stamp_legacy_counters(conn)
        _record(conn, VERSION_OFFSET + SCHEMA_VERSION)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return consolidated_version(conn)


def was_adopted(conn: sqlite3.Connection) -> bool:
    """True iff this DB reached the consolidated schema by adoption."""
    row = conn.execute(
        "SELECT 1 FROM schema_meta WHERE version = ?", (ADOPTION_VERSION,)
    ).fetchone()
    return row is not None


__all__ = [
    "ADOPTION_VERSION",
    "ALL_TABLES",
    "DOMAINS",
    "LEGACY_SHARED_STATE_VERSION",
    "LEGACY_SOURCES",
    "MIGRATIONS",
    "MIN_ADOPTABLE_LEGACY_VERSION",
    "SCHEMA_VERSION",
    "TABLES",
    "VENDOR_SIDECAR_DDL",
    "VENDOR_SIDECAR_TABLES",
    "VERSION_OFFSET",
    "SchemaAdoptionError",
    "apply_migrations",
    "consolidated_version",
    "ensure_vendor_sidecar_tables",
    "existing_objects",
    "missing_tables",
    "was_adopted",
]
