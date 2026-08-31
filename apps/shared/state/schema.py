"""SQLite schema + migrations for the shared state layer.

Schema is derivative: can be wiped and rebuilt from vendor DBs at any time.
Migrations are idempotent-at-the-statement-level; ``apply_migrations`` uses
a ``schema_meta`` table to skip already-applied versions.

Versioning: ``SCHEMA_VERSION`` is the target version. ``MIGRATIONS`` is a
list where index ``i`` is the SQL to take the schema from version ``i`` to
``i+1``. A fresh DB runs the full list.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION: int = 6


# --- migration 0 -> 1: initial schema ------------------------------------
_V1: list[str] = [
    # Identity table. Unwrapped facts per open-dj v0 strawman, section 6.
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
    # Vendor-ID round-trip map. Opaque vendor ids keyed by stable_id.
    """
    CREATE TABLE IF NOT EXISTS track_vendor_ids (
        stable_id  TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        vendor     TEXT NOT NULL,
        vendor_id  TEXT NOT NULL,
        PRIMARY KEY (stable_id, vendor)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_track_vendor_ids_vendor ON track_vendor_ids(vendor, vendor_id)",
    # Provenance-wrapped analysed fields (EAV).
    """
    CREATE TABLE IF NOT EXISTS track_fields (
        stable_id    TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        field_name   TEXT NOT NULL,
        value_json   TEXT NOT NULL,
        source       TEXT NOT NULL CHECK (source IN
                       ('mik','rekordbox','djay','serato','traktor',
                        'open-dj-tool','manual','inferred')),
        confidence   REAL CHECK (confidence IS NULL OR
                                 (confidence >= 0 AND confidence <= 1)),
        modified_at  TEXT NOT NULL,
        PRIMARY KEY (stable_id, field_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS track_field_history (
        stable_id     TEXT NOT NULL,
        field_name    TEXT NOT NULL,
        value_json    TEXT NOT NULL,
        source        TEXT NOT NULL,
        confidence    REAL,
        modified_at   TEXT NOT NULL,
        superseded_at TEXT NOT NULL,
        PRIMARY KEY (stable_id, field_name, superseded_at)
    )
    """,
    # Playlists + memberships.
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
    # Adapter registry -- last successful run per adapter.
    """
    CREATE TABLE IF NOT EXISTS adapters (
        adapter_id   TEXT PRIMARY KEY,
        last_run_at  TEXT,
        last_ok      INTEGER NOT NULL DEFAULT 0,
        notes        TEXT
    )
    """,
    # Durable event log. INFRA-01 bus floor.
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
    "CREATE INDEX IF NOT EXISTS idx_events_stable_id ON events(stable_id) WHERE stable_id IS NOT NULL",
]

# --- migration 1 -> 2: history append-only ---------------------------------
# [I1] The v1 ``track_field_history`` PK ``(stable_id, field_name,
# superseded_at)`` could silently overwrite a prior row if two rewrites
# landed in the same clock tick (e.g. frozen test clock or tight ingest
# loop). Replace the PK with a surrogate ``id INTEGER PRIMARY KEY
# AUTOINCREMENT`` so history is truly append-only, and keep the old triple
# as a non-unique index for lookup. Existing rows are preserved.
_V2: list[str] = [
    """
    CREATE TABLE track_field_history_v2 (
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
    """
    INSERT INTO track_field_history_v2(
        stable_id, field_name, value_json, source,
        confidence, modified_at, superseded_at
    )
    SELECT stable_id, field_name, value_json, source,
           confidence, modified_at, superseded_at
    FROM track_field_history
    """,
    "DROP TABLE track_field_history",
    "ALTER TABLE track_field_history_v2 RENAME TO track_field_history",
    "CREATE INDEX IF NOT EXISTS idx_track_field_history_lookup "
    "ON track_field_history(stable_id, field_name, superseded_at)",
]

# --- migration 2 -> 3: allow 'webui' as a track_fields source --------------
# The webui daemon persists PATCH /tracks/{stable_id} edits (rating / notes /
# tags) through StateWriter with source='webui'. The v1 CHECK constraint
# predates that writer, so rebuild track_fields with the widened source list
# (SQLite cannot ALTER a CHECK in place). Existing rows are preserved.
# Mirror of apps.shared.state.types.SOURCES -- kept in sync by test.
_V3: list[str] = [
    """
    CREATE TABLE track_fields_v3 (
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
    """
    INSERT INTO track_fields_v3(
        stable_id, field_name, value_json, source, confidence, modified_at
    )
    SELECT stable_id, field_name, value_json, source, confidence, modified_at
    FROM track_fields
    """,
    "DROP TABLE track_fields",
    "ALTER TABLE track_fields_v3 RENAME TO track_fields",
]

# --- migration 3 -> 4: multiple playable locations per track ---------------
# tracks.file_path stays the ingest/legacy primary path. track_locations
# holds extra copies (this machine, a remote host, a lower-bitrate
# transcode). The play path picks one; the frontend never sees the list.
_V4: list[str] = [
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
    """
    INSERT OR IGNORE INTO track_locations(
        stable_id, kind, role, file_path, created_at, updated_at
    )
    SELECT stable_id, 'local', 'primary', file_path, updated_at, updated_at
    FROM tracks
    WHERE file_path IS NOT NULL AND file_path != ''
    """,
]

# Live agentbox state.db already had schema_meta version 4 from an
# out-of-band bump that did not create track_locations. Re-run the same
# idempotent statements as 4 -> 5 so those DBs catch up. Fresh DBs run
# both steps; IF NOT EXISTS / OR IGNORE keep the second a no-op.
_V5: list[str] = _V4

# --- migration 5 -> 6: sync-safe schema (CLOUDSYNC) ------------------------
# Contract: specs/design_decision_05.md, rationale specs/design_decision_04.md.
# Three facts blocked hub sync: track_locations had an INTEGER PRIMARY KEY
# AUTOINCREMENT that collides across machines, no synced table carried
# updated_at / origin_device_id / deleted_at, and there were no machines or
# policy tables. Pure SQL, no Python, so it rides the existing MIGRATIONS
# machinery.
#
# Amended IN PLACE for round 2 by specs/design_decision_08.md points 1 and 2
# (v6 has never touched a real database; v7 is reserved for the auth branch).
#
# Four deliberate readings of the ADRs, recorded here so a reader does not
# have to diff the docs:
# 1. tracks, playlists and track_locations ALREADY carry ``updated_at TEXT
#    NOT NULL`` from v1/v4, so v6 adds only the two missing sync columns
#    there. SQLite has no ALTER TABLE ADD COLUMN IF NOT EXISTS; re-adding
#    would abort the migration. The ADR's "eighteen ALTERs" is thirteen.
#    Those three tables never produce the NULL-updated_at legacy row of
#    ADR 04 c7 because the column was always stamped.
# 2. ``location_id`` is NOT NULL with a minting DEFAULT. The ADR says
#    "TEXT PRIMARY KEY"; a bare TEXT PRIMARY KEY in SQLite still accepts
#    NULL (legacy quirk), so any writer that does not name the column would
#    silently insert unsyncable NULL-keyed rows. NOT NULL + DEFAULT makes
#    that impossible instead of merely unlikely.
# 3. ``track_locations.machine_id`` is DDL-NULLABLE, against ADR 08 point 1's
#    "NOT NULL". A pure-SQL migration cannot know this machine's id (it lives
#    in ``<data-dir>/machine-id``, deliberately outside the DB), so the
#    v5 -> v6 INSERT ... SELECT has no value to copy for pre-existing rows.
#    Real NOT NULL would therefore make ``apply_migrations`` unusable on its
#    own, and it is called directly by the CLI, the webui and ~20 test
#    modules. The column is instead backfilled immediately after migration by
#    :func:`apps.shared.state.sync_stamp.backfill_local_machine_id`, which
#    ``db.open_rw`` invokes on every writable open, and the NOT NULL
#    invariant is enforced by the tripwire in
#    ``tests/shared/state/test_writer_sync_stamps.py`` rather than by DDL.
#    Every writer in this package supplies the column explicitly.
# 4. ``local_changelog`` is the spoke-side twin of ``hub_changelog``
#    (ADR 08 point 3): same shape, machine-local, never synced. It is what
#    ``sync_state.last_push_seq`` fences against, replacing the wall-clock
#    push watermark that lost rows under clock skew (round 1 finding 3).
_V6: list[str] = [
    # --- fleet identity + per-machine policy (synced set, ADR 04 c8) ------
    """
    CREATE TABLE IF NOT EXISTS machines (
        machine_id  TEXT PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        platform    TEXT NOT NULL CHECK
                      (platform IN ('macos','windows','linux')),
        is_hub      INTEGER NOT NULL DEFAULT 0,
        data_root   TEXT,
        first_seen  TEXT NOT NULL,
        last_seen   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_policies (
        machine_id       TEXT NOT NULL
                           REFERENCES machines(machine_id) ON DELETE CASCADE,
        asset_kind       TEXT NOT NULL CHECK (asset_kind IN
                           ('audio','stem_bundle','anlz_cache','vocal_cache')),
        mode             TEXT NOT NULL CHECK (mode IN
                           ('pinned','cached','stream','excluded')),
        cache_budget_mb  INTEGER,
        updated_at       TEXT,
        origin_device_id TEXT,
        deleted_at       TEXT,
        PRIMARY KEY (machine_id, asset_kind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playlist_pins (
        machine_id       TEXT NOT NULL
                           REFERENCES machines(machine_id) ON DELETE CASCADE,
        playlist_id      TEXT NOT NULL
                           REFERENCES playlists(playlist_id) ON DELETE CASCADE,
        mode             TEXT NOT NULL CHECK (mode IN
                           ('pinned','cached','stream','excluded')),
        updated_at       TEXT,
        origin_device_id TEXT,
        deleted_at       TEXT,
        PRIMARY KEY (machine_id, playlist_id)
    )
    """,
    # --- machine-local sync bookkeeping (never synced, ADR 04 c8) --------
    """
    CREATE TABLE IF NOT EXISTS sync_state (
        peer           TEXT PRIMARY KEY,
        last_push_seq  INTEGER NOT NULL DEFAULT 0,
        last_pull_seq  INTEGER NOT NULL DEFAULT 0,
        last_sync_at   TEXT
    )
    """,
    # Hub-local. AUTOINCREMENT is correct here precisely because this table
    # never crosses a machine boundary -- seq is the pull watermark.
    """
    CREATE TABLE IF NOT EXISTS hub_changelog (
        seq              INTEGER PRIMARY KEY AUTOINCREMENT,
        table_name       TEXT NOT NULL,
        row_pk           TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        origin_device_id TEXT NOT NULL,
        received_at      TEXT NOT NULL
    )
    """,
    # Spoke-local. Appended by apps.shared.state.sync_stamp on every write to
    # a synced table; ``sync_state.last_push_seq`` is the floor into it, so a
    # skewed wall clock can no longer raise the push watermark past a local
    # edit and lose it (round 1 finding 3). Never crosses a machine boundary.
    """
    CREATE TABLE IF NOT EXISTS local_changelog (
        seq              INTEGER PRIMARY KEY AUTOINCREMENT,
        table_name       TEXT NOT NULL,
        row_pk           TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        origin_device_id TEXT NOT NULL,
        received_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_local_changelog_table "
    "ON local_changelog(table_name, row_pk)",
    # --- sync columns on the existing synced tables ----------------------
    "ALTER TABLE tracks ADD COLUMN origin_device_id TEXT",
    "ALTER TABLE tracks ADD COLUMN deleted_at TEXT",
    "ALTER TABLE track_vendor_ids ADD COLUMN updated_at TEXT",
    "ALTER TABLE track_vendor_ids ADD COLUMN origin_device_id TEXT",
    "ALTER TABLE track_vendor_ids ADD COLUMN deleted_at TEXT",
    "ALTER TABLE track_fields ADD COLUMN updated_at TEXT",
    "ALTER TABLE track_fields ADD COLUMN origin_device_id TEXT",
    "ALTER TABLE track_fields ADD COLUMN deleted_at TEXT",
    "ALTER TABLE playlists ADD COLUMN origin_device_id TEXT",
    "ALTER TABLE playlists ADD COLUMN deleted_at TEXT",
    "ALTER TABLE playlist_memberships ADD COLUMN updated_at TEXT",
    "ALTER TABLE playlist_memberships ADD COLUMN origin_device_id TEXT",
    "ALTER TABLE playlist_memberships ADD COLUMN deleted_at TEXT",
    # --- track_locations PK rebuild: INTEGER AUTOINCREMENT -> uuid4 hex --
    # Same columns, same CHECKs, plus the sync trio and ``machine_id``.
    # Nothing references track_locations, so the drop-and-rename needs no FK
    # dance. ``machine_id`` makes each machine's view of where a file lives
    # its own row (ADR 08 point 1): ``file_path``, ``available``,
    # ``probed_at`` and ``venue_*`` are per-machine facts, and forcing them
    # into one global row is what produced round 1 findings 1 and 7a.
    """
    CREATE TABLE track_locations_v6 (
        location_id      TEXT PRIMARY KEY NOT NULL
                           DEFAULT (lower(hex(randomblob(16)))),
        stable_id        TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        machine_id       TEXT REFERENCES machines(machine_id),
        kind             TEXT NOT NULL CHECK (kind IN ('local', 'remote')),
        role             TEXT NOT NULL DEFAULT 'alternate'
                           CHECK (role IN ('primary', 'alternate')),
        file_path        TEXT,
        remote_url       TEXT,
        venue_key        TEXT,
        venue_rank       INTEGER,
        available        INTEGER NOT NULL DEFAULT 0,
        probed_at        TEXT,
        content_hash     TEXT,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        origin_device_id TEXT,
        deleted_at       TEXT,
        CHECK (
            (file_path IS NOT NULL AND file_path != '')
            OR (remote_url IS NOT NULL AND remote_url != '')
        )
    )
    """,
    # machine_id is left NULL here and backfilled by
    # sync_stamp.backfill_local_machine_id -- see reading 3 above.
    """
    INSERT INTO track_locations_v6(
        location_id, stable_id, kind, role, file_path, remote_url,
        venue_key, venue_rank, available, probed_at, content_hash,
        created_at, updated_at
    )
    SELECT lower(hex(randomblob(16))), stable_id, kind, role, file_path,
           remote_url, venue_key, venue_rank, available, probed_at,
           content_hash, created_at, updated_at
    FROM track_locations
    """,
    "DROP TABLE track_locations",
    "ALTER TABLE track_locations_v6 RENAME TO track_locations",
    "CREATE INDEX IF NOT EXISTS idx_track_locations_stable "
    "ON track_locations(stable_id)",
    # machine_id joins the logical identity: two machines holding the same
    # (stable_id, kind, path) now hold two rows, so the hub upsert merges by
    # LWW instead of wedging the push with a UNIQUE-violation 409.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_track_locations_path "
    "ON track_locations(stable_id, machine_id, kind, file_path) "
    "WHERE file_path IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_track_locations_url "
    "ON track_locations(stable_id, machine_id, kind, remote_url) "
    "WHERE remote_url IS NOT NULL",
]

# Each element is the set of SQL statements that take schema from N to N+1.
# MIGRATIONS[0] runs when going from v0 (empty) to v1.
MIGRATIONS: list[list[str]] = [_V1, _V2, _V3, _V4, _V5, _V6]


def _ensure_meta(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT    NOT NULL
        )
        """
    )


def _current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_meta").fetchone()
    return int(row[0]) if row is not None else 0


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Bring ``conn`` up to :data:`SCHEMA_VERSION`; return the resulting version.

    Idempotent. Every unapplied migration step runs inside its own
    transaction and records a row in ``schema_meta`` on success.
    """
    _ensure_meta(conn)
    current = _current_version(conn)
    if current >= SCHEMA_VERSION:
        return current

    for step_idx in range(current, SCHEMA_VERSION):
        statements = MIGRATIONS[step_idx]
        target_version = step_idx + 1
        # Use a manual BEGIN -- sqlite3's default transaction handling does
        # not wrap DDL cleanly unless we're explicit. Safe in isolation:
        # caller owns the connection.
        conn.execute("BEGIN")
        try:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
                (target_version, datetime.now(timezone.utc).isoformat()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return _current_version(conn)


TABLES: tuple[str, ...] = (
    "tracks",
    "track_vendor_ids",
    "track_fields",
    "track_field_history",
    "playlists",
    "playlist_memberships",
    "adapters",
    "events",
    "track_locations",
    # v6 (CLOUDSYNC, specs/design_decision_05.md)
    "machines",
    "sync_policies",
    "playlist_pins",
    "sync_state",
    "hub_changelog",
    "local_changelog",
)
"""Domain tables created by :data:`MIGRATIONS`. ``schema_meta`` is
intentionally excluded -- it is infrastructure, not domain data."""


FOREIGN_AUTHORITY_TABLES: tuple[str, ...] = (
    # apps/shared/pairings/schema_sql.py
    "pairings",
    "smartlists",
    # apps/shared/play_orders/schema.py (private version counter)
    "play_orders",
    "play_order_entries",
    "play_orders_schema_meta",
    # apps/launcher/scripts/bootstrap_db.py
    "tracks_fts",
    "tracks_fts_config",
    "tracks_fts_content",
    "tracks_fts_data",
    "tracks_fts_docsize",
    "tracks_fts_idx",
    "tracks_frecency",
)
"""Tables this module does NOT create but that legitimately live in the same
file, written by the other three schema authorities (spec section 1.3 of
``specs/cloudsync-spec.md``). Declared so the drift tripwire can pass on a
fully-provisioned DB while still failing on a genuinely undeclared table.
D2 folds these into :data:`MIGRATIONS` in a later round; until then this
tuple is the honest inventory, not an aspiration."""


INFRASTRUCTURE_TABLES: tuple[str, ...] = ("schema_meta",)
"""Migration bookkeeping owned by this module."""


ALL_KNOWN_TABLES: frozenset[str] = frozenset(
    TABLES + FOREIGN_AUTHORITY_TABLES + INFRASTRUCTURE_TABLES
)
"""Every non-``sqlite_*`` table name expected in a fully-provisioned state DB.
Anything in ``sqlite_master`` outside this set is undeclared drift."""
