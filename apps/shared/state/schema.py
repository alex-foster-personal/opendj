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

SCHEMA_VERSION: int = 4


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

# --- migration 3 -> 4: Google sign-in (users + auth_sessions) --------------
# The webui gained a user bauble that signs in with Google (openid/email/
# profile only). Two tables, both keyed off Google's ``sub`` claim -- the
# only identifier Google guarantees is stable and never reused. Email is
# NOT the key: Google account emails can change.
#
# ``auth_sessions.session_token_sha256`` stores a hash, never the bearer
# value the browser holds, so a stolen DB cannot be replayed as a cookie.
# ``refresh_token`` is the Google grant that produced the session; it stays
# server-side and is never exposed over the API.
_V4: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS users (
        google_sub  TEXT PRIMARY KEY,
        email       TEXT NOT NULL,
        name        TEXT,
        avatar_url  TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)",
    """
    CREATE TABLE IF NOT EXISTS auth_sessions (
        session_token_sha256 TEXT PRIMARY KEY,
        google_sub           TEXT NOT NULL
                               REFERENCES users(google_sub) ON DELETE CASCADE,
        refresh_token        TEXT,
        access_token         TEXT,
        access_expires_at    TEXT,
        created_at           TEXT NOT NULL,
        last_seen_at         TEXT NOT NULL,
        expires_at           TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_auth_sessions_sub "
    "ON auth_sessions(google_sub)",
    "CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires "
    "ON auth_sessions(expires_at)",
]

# Each element is the set of SQL statements that take schema from N to N+1.
# MIGRATIONS[0] runs when going from v0 (empty) to v1.
MIGRATIONS: list[list[str]] = [_V1, _V2, _V3, _V4]


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
    "users",
    "auth_sessions",
)
"""Domain tables. v1 created everything up to ``events``; v4 added ``users``
and ``auth_sessions``. ``schema_meta`` is intentionally excluded -- it is
infrastructure, not domain data."""
