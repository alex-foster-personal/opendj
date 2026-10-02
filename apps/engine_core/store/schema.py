"""The ONE authoritative SQLite schema for the Open DJ store.

Today ``state.db`` is bootstrapped from ~17 places: only
``apps/shared/state/schema.py`` participates in a versioned migration
ladder, and everything else runs ad-hoc ``CREATE TABLE IF NOT EXISTS``
on first use from whichever module happens to own the domain. This
module is the consolidation target: every table and index the engine
owns, declared once, with a comment naming the legacy file each
definition came from.

Two groups, because they do not mean the same thing:

* **durable** (:data:`DOMAINS`, applied by :func:`apply_migrations` to
  ``state.db``) -- rows that cannot be recomputed. Identity, provenance,
  playlists, analysis, and human judgment such as duplicate clusters and
  tag provenance. This ladder never wipes.
* **regenerable caches** (:data:`CACHE_DOMAINS`, applied by
  :func:`apply_cache_migrations` to a separate ``cache.db``) -- rows
  derived from audio files, where deleting the file is a legal recovery
  move. Kept out of the durable ladder so that promise stays true. See
  ``REPORT.md``, "Durability split".

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
* **existing DB** -- adoption. The runner creates only the missing
  objects, replays the legacy backfills that are data-bearing
  (idempotent), and records the adoption as its own
  :data:`ADOPTION_VERSION` row in ``schema_meta``.

Adoption is gated BEFORE anything is written, and the gate reads live
shapes rather than trusting the counter:

* :func:`_assert_adoptable` refuses a DB below
  :data:`MIN_ADOPTABLE_LEGACY_VERSION`, and independently probes every
  table the legacy ladder repairs by REBUILDING it (``track_fields``'
  widened CHECK, ``track_field_history``'s surrogate PK). This runner
  only creates missing objects, so it can never repair those shapes.
  The probes run whatever the counter says: an absent or truncated
  ``schema_meta`` reads the same as a fresh file, so the counter cannot
  be the gate.
* :func:`_audit_existing_shapes` compares every pre-existing object the
  ladder would create against the shape it would create, and refuses on
  a mismatch. Necessary because every statement here is ``IF NOT
  EXISTS``, which makes name-presence and shape-presence different
  facts -- including the table/view case, where a view silently absorbs
  a ``CREATE TABLE``.

The transaction is ``BEGIN IMMEDIATE`` with a runner-owned
:data:`BUSY_TIMEOUT_MS`, because this is a read-then-write sequence and
a deferred lock upgrade is the one wait SQLite will not retry.

Scope note: tables that legacy code creates inside a *vendor* database
(rekordbox ``master.db``) are declared here too, as
:data:`VENDOR_SIDECAR_DDL`, so the DDL has a single home -- but they are
deliberately NOT part of the state-DB ladder. See ``REPORT.md``.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from functools import cache

from apps.analysis.queue_stale import STALE_TABLES_SQL
from apps.analysis.queue_store import QUEUE_TABLES_SQL
from apps.shared.pairings.schema_sql import migrate_smartlists_deleted_at
from apps.shared.state.migrations_v9 import ENROLLED_VIA_VALUES

_WHITESPACE_RE = re.compile(r"\s+")
_IF_NOT_EXISTS_RE = re.compile(r"\bIF\s+NOT\s+EXISTS\b", re.IGNORECASE)

# --- version counters -----------------------------------------------------

SCHEMA_VERSION: int = 12
"""Target version of the consolidated ladder (index into :data:`MIGRATIONS`)."""

VERSION_OFFSET: int = 1000
"""Consolidated versions are stamped into ``schema_meta`` at this offset so
they never collide with the legacy shared-state counter (1..5)."""

ADOPTION_VERSION: int = VERSION_OFFSET
"""Marker row written when the runner ADOPTED a pre-existing DB rather than
creating it from scratch. Distinct from the ``VERSION_OFFSET + n`` rows so a
reader can tell "this file predates consolidation" from "this file was born
consolidated"."""

LEGACY_SHARED_STATE_VERSION: int = 7
"""Terminal version of ``apps/shared/state/schema.py``'s own ladder."""

BUSY_TIMEOUT_MS: int = 5000
"""How long the runner waits for a competing writer before giving up.

Stamped onto the connection by :func:`apply_migrations` rather than inherited.
``sqlite3.connect`` has a caller-side ``timeout`` argument that defaults to 5s
but is 0 for any caller that tunes its own connections -- and a migration that
gives up in 0.000s because of how someone else opened the file is a hidden
default, not a policy. The runner owns this one."""

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
    # Flat on purpose, here and below: legacy v6 ALTER-splices the sync
    # columns into the stored CREATE text, and the parity gate compares the
    # normalized bytes -- including SQLite's odd " ," splice spacing on
    # tracks. Pretty-printing these would fail the gate.
    "CREATE TABLE IF NOT EXISTS tracks ( stable_id TEXT PRIMARY KEY, "
    "stable_id_tier TEXT NOT NULL CHECK "
    "(stable_id_tier IN ('isrc','fingerprint','inferred')), title TEXT, "
    "artists_json TEXT, album TEXT, isrc TEXT, duration_ms INTEGER, "
    "file_path TEXT, content_hash TEXT, created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL , origin_device_id TEXT, deleted_at TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_isrc ON tracks(isrc) WHERE isrc IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_tracks_file_path ON tracks(file_path)",
    "CREATE TABLE IF NOT EXISTS track_vendor_ids ( stable_id TEXT NOT NULL "
    "REFERENCES tracks(stable_id) ON DELETE CASCADE, vendor TEXT NOT NULL, "
    "vendor_id TEXT NOT NULL, updated_at TEXT, origin_device_id TEXT, "
    "deleted_at TEXT, PRIMARY KEY (stable_id, vendor) )",
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
        updated_at   TEXT,
        origin_device_id TEXT,
        deleted_at   TEXT,
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
    "CREATE TABLE IF NOT EXISTS playlists ( playlist_id TEXT PRIMARY KEY, "
    "name TEXT NOT NULL, vendor TEXT NOT NULL, vendor_pl_id TEXT NOT NULL, "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
    "origin_device_id TEXT, deleted_at TEXT, "
    "forbid_duplicates INTEGER NOT NULL DEFAULT 0, "
    "UNIQUE (vendor, vendor_pl_id) )",
    # Legacy v13 ALTER-splices item_id / order_key after deleted_at, still
    # before the table-level PRIMARY KEY. Do not pretty-print; the parity
    # gate compares normalised sqlite_master bytes.
    "CREATE TABLE IF NOT EXISTS playlist_memberships ( playlist_id TEXT NOT "
    "NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE, stable_id TEXT "
    "NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE, position "
    "INTEGER NOT NULL, updated_at TEXT, origin_device_id TEXT, deleted_at "
    "TEXT, item_id TEXT, order_key TEXT, PRIMARY KEY (playlist_id, position) )",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_playlist_memberships_item_id "
    "ON playlist_memberships(item_id)",
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
    "CREATE TABLE IF NOT EXISTS track_locations ( location_id TEXT PRIMARY "
    "KEY NOT NULL DEFAULT (lower(hex(randomblob(16)))), stable_id TEXT NOT "
    "NULL REFERENCES tracks(stable_id) ON DELETE CASCADE, machine_id TEXT "
    "REFERENCES machines(machine_id), kind TEXT NOT NULL CHECK (kind IN "
    "('local', 'remote')), role TEXT NOT NULL DEFAULT 'alternate' CHECK "
    "(role IN ('primary', 'alternate')), file_path TEXT, remote_url TEXT, "
    "venue_key TEXT, venue_rank INTEGER, available INTEGER NOT NULL DEFAULT "
    "0, probed_at TEXT, content_hash TEXT, created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, origin_device_id TEXT, deleted_at TEXT, "
    "CHECK ( (file_path IS NOT NULL AND file_path != '') OR (remote_url IS "
    "NOT NULL AND remote_url != '') ) )",
    "CREATE INDEX IF NOT EXISTS idx_track_locations_stable "
    "ON track_locations(stable_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_track_locations_path "
    "ON track_locations(stable_id, machine_id, kind, file_path) "
    "WHERE file_path IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_track_locations_url "
    "ON track_locations(stable_id, machine_id, kind, remote_url) "
    "WHERE remote_url IS NOT NULL",
    # Legacy v6: Google sign-in. Keyed on Google's ``sub``, the only
    # identifier Google guarantees stable; email can change. The session
    # token is stored as a sha256 so a leaked DB is not a bag of cookies.
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
)


# ==========================================================================
# DOMAIN: sync infrastructure (CLOUDSYNC hub sync, legacy v6)
# Legacy source: apps/shared/state/schema.py  (_V6, amended per
# specs/design_decision_08.md: per-machine track_locations identity, spoke
# local_changelog). hub_changelog and local_changelog keep INTEGER
# AUTOINCREMENT keys because both are machine-local by contract (ADR 04
# sync set) and never cross machines.
# ==========================================================================

_SYNC_INFRA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS machines ( machine_id TEXT PRIMARY KEY, "
    "name TEXT NOT NULL UNIQUE, platform TEXT NOT NULL CHECK (platform IN "
    "('macos','windows','linux')), is_hub INTEGER NOT NULL DEFAULT 0, "
    "data_root TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL )",
    # The asset_kind CHECK carries the SIX kinds legacy _V10 rebuilds the
    # table to, not the four v7 created it with: the legacy side of the parity
    # gate runs the whole ladder, so this text is compared against the rebuilt
    # shape. Character-for-character with
    # apps/shared/state/migrations_v10.ASSET_KIND_CHECK_VALUES.
    "CREATE TABLE IF NOT EXISTS sync_policies ( machine_id TEXT NOT NULL "
    "REFERENCES machines(machine_id) ON DELETE CASCADE, asset_kind TEXT NOT "
    "NULL CHECK (asset_kind IN ('audio','stem_bundle','anlz_cache',"
    "'vocal_cache','lyrics_cache','karaoke_words')), mode TEXT NOT NULL CHECK "
    "(mode IN ('pinned','cached','stream','excluded')), cache_budget_mb "
    "INTEGER, updated_at TEXT, origin_device_id TEXT, deleted_at TEXT, "
    "PRIMARY KEY (machine_id, asset_kind) )",
    "CREATE TABLE IF NOT EXISTS playlist_pins ( machine_id TEXT NOT NULL "
    "REFERENCES machines(machine_id) ON DELETE CASCADE, playlist_id TEXT "
    "NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE, mode "
    "TEXT NOT NULL CHECK (mode IN ('pinned','cached','stream','excluded')), "
    "updated_at TEXT, origin_device_id TEXT, deleted_at TEXT, PRIMARY KEY "
    "(machine_id, playlist_id) )",
    "CREATE TABLE IF NOT EXISTS sync_state ( peer TEXT PRIMARY KEY, "
    "last_push_seq INTEGER NOT NULL DEFAULT 0, last_pull_seq INTEGER NOT "
    "NULL DEFAULT 0, last_sync_at TEXT, peer_generation TEXT )",
    "CREATE TABLE IF NOT EXISTS hub_changelog ( seq INTEGER PRIMARY KEY "
    "AUTOINCREMENT, table_name TEXT NOT NULL, row_pk TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, origin_device_id TEXT NOT NULL, received_at "
    "TEXT NOT NULL )",
    "CREATE TABLE IF NOT EXISTS local_changelog ( seq INTEGER PRIMARY KEY "
    "AUTOINCREMENT, table_name TEXT NOT NULL, row_pk TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, origin_device_id TEXT NOT NULL, received_at "
    "TEXT NOT NULL )",
    "CREATE INDEX IF NOT EXISTS idx_local_changelog_table "
    "ON local_changelog(table_name, row_pk)",
    # Legacy v16 (apps/shared/state/migrations_v16.py): the hub-side twin of
    # idx_local_changelog_table.
    "CREATE INDEX IF NOT EXISTS idx_hub_changelog_table "
    "ON hub_changelog(table_name, row_pk)",
)


# ==========================================================================
# DOMAIN: CloudSync per-table write tokens (legacy v21, issue #4396)
# Legacy source: apps/shared/state/migrations_v21.py. Its own rung, not an
# append to _SYNC_INFRA/_V1: an install already stamped at v1 never re-runs
# rung 1 (see the _V2 docstring below), so appending here would leave the
# table absent on every pre-existing consolidated install.
#
# Only the table is mirrored, not the write-token TRIGGERS
# (apps.shared.state.migrations_v21.EXPECTED_TRIGGERS) or their seed INSERTs:
# the table/index equivalence gate (tests/engine_core/test_store_schema.py)
# this module is checked against does not compare triggers, and unlike this
# domain's CREATE TABLE the trigger CREATEs and seed INSERTs are not safely
# re-runnable on an already-adopted DB (no CREATE TRIGGER IF NOT EXISTS twin
# here, and a seed INSERT would collide with rows the legacy ladder already
# wrote). They stay out of this dormant consolidation target until it
# actually needs them.
# ==========================================================================

_SYNC_WRITE_TOKENS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS sync_write_tokens ( table_name TEXT PRIMARY "
    "KEY, token BLOB NOT NULL ) WITHOUT ROWID",
)


# ==========================================================================
# DOMAIN: machine enrollment (who OWNS a machine, legacy v9)
# Legacy source: apps/shared/state/migrations_v9.py (_V9, specs/
# design_decision_12.md). Its own domain rather than more rows in
# _SYNC_INFRA: those tables carry replication STATE and every one of them
# rides the sync set, whereas neither table here is ever synced -- the hub
# that performed the enrollment is the only writer (ADR 12 reading 1). One
# domain, one answer to "does this cross machines".
# ==========================================================================

#: The provenance vocabulary, read from the legacy rung rather than retyped.
#: Claude review, PR #1648 (P3): migrations_v9 builds its CHECK from this
#: tuple explicitly "so the provenance list has one home rather than a copy
#: that can drift from the constraint", and enrollment_table_docs repeats
#: that claim to readers -- while this mirror wrote the three values out as a
#: literal, which is the copy the docs deny exists. A fourth provenance would
#: have left a consolidated database rejecting those rows with an
#: IntegrityError naming nothing.
#:
#: The one import this module takes from a legacy bootstrap, and deliberately
#: narrow: a VOCABULARY, not DDL. The table text stays a verbatim lift like
#: every other domain here, because tests/engine_core/test_store_schema.py
#: compares it against the legacy ladder's real output object by object, and
#: importing the statements would make that gate compare a thing with itself.
_ENROLLED_VIA_SQL: str = ",".join(f"'{value}'" for value in ENROLLED_VIA_VALUES)

_ENROLLMENT: tuple[str, ...] = (
    f"""
    CREATE TABLE IF NOT EXISTS machine_owners (
        machine_id      TEXT PRIMARY KEY
                          REFERENCES machines(machine_id) ON DELETE CASCADE,
        google_sub      TEXT NOT NULL
                          REFERENCES users(google_sub) ON DELETE CASCADE,
        hub_machine_id  TEXT NOT NULL,
        enrolled_at     TEXT NOT NULL,
        enrolled_via    TEXT NOT NULL CHECK
                          (enrolled_via IN ({_ENROLLED_VIA_SQL})),
        revoked_at      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_machine_owners_sub "
    "ON machine_owners(google_sub)",
    """
    CREATE TABLE IF NOT EXISTS enrollment_grants (
        grant_token_sha256  TEXT PRIMARY KEY,
        google_sub          TEXT NOT NULL
                              REFERENCES users(google_sub) ON DELETE CASCADE,
        created_at          TEXT NOT NULL,
        expires_at          TEXT NOT NULL,
        redeemed_at         TEXT,
        redeemed_machine_id TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_enrollment_grants_expires "
    "ON enrollment_grants(expires_at)",
)


# ==========================================================================
# DOMAIN: per-machine sync credentials (legacy v11, ADR 12 amendment)
# Legacy source: apps/shared/state/migrations_v11.py (_V11). Its own domain
# for the reason _ENROLLMENT gives: never synced, hub is the only writer.
# ==========================================================================

_CREDENTIALS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS machine_credentials (
        machine_id         TEXT PRIMARY KEY
                             REFERENCES machines(machine_id) ON DELETE CASCADE,
        credential_sha256  TEXT NOT NULL UNIQUE,
        minted_at          TEXT NOT NULL
    )
    """,
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
# DOMAIN: native-analysis v1 -- canonical pointer, read-time projection, and
# the persisted per-lane source default.
# Legacy sources: apps/analysis/store.py (_ANALYSIS_TABLES_SQL) and
# apps/analysis/selection.py (_SOURCE_DEFAULT_TABLE_SQL, created on the
# first promotion).
#
# Kept as its own tuple rather than appended to _ANALYSIS because it is BOTH
# the fresh-DB DDL and the body of migration v2: appending it to _V1 alone
# would have meant a state.db already stamped at the current version never
# gained these tables, since apply_migrations returns early at
# `current >= SCHEMA_VERSION`. Reproduced Wed 9 Sep 2026 before this split.
# ==========================================================================

_NATIVE_ANALYSIS_V1: tuple[str, ...] = (
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
    # only half of the selection surface that survives a relaunch. Created by
    # apps/analysis/selection.py on first use; declared here so schema
    # adoption, drift checks and the fresh-database inventory all know it.
    """
    CREATE TABLE IF NOT EXISTS analysis_source_default (
        lane        TEXT PRIMARY KEY,
        source      TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    # The backfill queue (spec section 3 "Queue") and the staleness table,
    # SPLICED IN from the modules that own them rather than restated. They
    # were written out here as well until Thu 10 Sep 2026 and the two copies
    # were byte-identical, which is exactly the drift sync_drift_lint exists
    # to catch: a column added on one side and not the other provisions a
    # fresh database differently from a running one. One definition, two
    # readers. The import direction is owner -> registry (the same direction
    # as ENROLLED_VIA_VALUES above); neither analysis module imports
    # engine_core, so there is no cycle.
    *QUEUE_TABLES_SQL,
    *STALE_TABLES_SQL,
)


# ==========================================================================
# DOMAIN: analysis retention -- availability dimension, unmatched staging,
# energy time series, per-field verification provenance
# Legacy source: apps/shared/state/schema.py (_V8, the af--analysis-retention
# migration). Views are deliberately NOT mirrored here: this consolidated
# module tracks tables/indexes only (see DOMAINS/TABLES docstrings); no other
# domain here carries a view either.
# ==========================================================================

_ANALYSIS_RETENTION: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS track_availability (
        stable_id     TEXT PRIMARY KEY REFERENCES tracks(stable_id) ON DELETE CASCADE,
        state         TEXT NOT NULL CHECK (state IN
                        ('present','absent','awaiting_volume','streaming')),
        checked_path  TEXT,
        checked_at    TEXT NOT NULL
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_track_availability_state "
        "ON track_availability(state)"
    ),
    """
    CREATE TABLE IF NOT EXISTS unmatched_source_analysis (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        source             TEXT NOT NULL CHECK (source IN
                             ('mik','rekordbox','djay','serato','traktor',
                              'open-dj-tool','manual','inferred','webui')),
        source_row_id      TEXT NOT NULL,
        field_name         TEXT NOT NULL,
        value_json         TEXT NOT NULL,
        unmatched_reason   TEXT NOT NULL CHECK (unmatched_reason IN
                             ('no_candidate','ambiguous_candidates',
                              'lost_collision')),
        confidence         REAL CHECK (confidence IS NULL OR
                                       (confidence >= 0 AND confidence <= 1)),
        title              TEXT,
        artist             TEXT,
        album              TEXT,
        isrc               TEXT,
        duration_ms        INTEGER,
        source_path        TEXT,
        modified_at        TEXT NOT NULL,
        imported_at        TEXT NOT NULL,
        promoted_stable_id TEXT REFERENCES tracks(stable_id) ON DELETE SET NULL,
        promoted_at        TEXT,
        UNIQUE (source, source_row_id, field_name)
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_unmatched_source_analysis_pending "
        "ON unmatched_source_analysis(source, field_name) "
        "WHERE promoted_stable_id IS NULL"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_unmatched_source_analysis_promoted "
        "ON unmatched_source_analysis(promoted_stable_id) "
        "WHERE promoted_stable_id IS NOT NULL"
    ),
    """
    CREATE TABLE IF NOT EXISTS track_energy_segments (
        stable_id   TEXT NOT NULL REFERENCES tracks(stable_id) ON DELETE CASCADE,
        seq         INTEGER NOT NULL,
        start_ms    INTEGER NOT NULL CHECK (start_ms >= 0),
        length_ms   INTEGER NOT NULL CHECK (length_ms > 0),
        energy      INTEGER NOT NULL CHECK (energy BETWEEN 1 AND 10),
        source      TEXT NOT NULL CHECK (source IN
                      ('mik','rekordbox','djay','serato','traktor',
                       'open-dj-tool','manual','inferred','webui')),
        confidence  REAL CHECK (confidence IS NULL OR
                                (confidence >= 0 AND confidence <= 1)),
        start_clamped INTEGER NOT NULL DEFAULT 0
                        CHECK (start_clamped IN (0, 1)),
        modified_at TEXT NOT NULL,
        PRIMARY KEY (stable_id, source, seq)
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_track_energy_segments_start "
        "ON track_energy_segments(stable_id, start_ms)"
    ),
    """
    CREATE TABLE IF NOT EXISTS analysis_field_verification (
        source      TEXT NOT NULL,
        field_name  TEXT NOT NULL,
        status      TEXT NOT NULL,
        basis       TEXT NOT NULL CHECK (basis IN
                      ('cross_source','single_source','unverified')),
        normaliser  TEXT,
        checked_at  TEXT,
        verified_by TEXT,
        overridden  INTEGER NOT NULL DEFAULT 0 CHECK (overridden IN (0, 1)),
        recorded_at TEXT NOT NULL,
        PRIMARY KEY (source, field_name)
    )
    """,
)



# ==========================================================================
# DOMAIN: karaoke lyrics -- one verdict row per track
# Legacy source: apps/shared/state/migrations_v10.py (_V10, specs/
# karaoke-lyrics-operational-plan.md D13.1). Same consolidation rule as
# everywhere else in this file: the shapes below are the legacy ladder's
# output, reproduced verbatim.
#
# ONE deliberate divergence from the legacy text, and it is a creation-time
# flag rather than a shape: the legacy ladder writes a BARE ``CREATE TABLE``
# and a BARE ``CREATE INDEX`` so a stale ``idx_lyric_verdict_red`` left on a
# renamed-aside branch table fails the migration loudly (migrations_v10.py
# reading 2). This module cannot do that: every statement here is replayed
# against ALREADY-provisioned databases by the adoption path, so it must be
# ``IF NOT EXISTS`` or adoption breaks on every real file. ``normalize_object_
# sql`` strips the flag before comparing, so the parity gate is unaffected.
# ==========================================================================

_LYRICS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS lyric_verdict (
        stable_id          TEXT PRIMARY KEY REFERENCES tracks(stable_id) ON DELETE CASCADE,
        verdict            TEXT NOT NULL CHECK (verdict IN
                             ('vocal','sparse','no-lyrics','unknown')),
        coverage_pct       REAL,
        source             TEXT,
        language_iso3      TEXT,
        n_words            INTEGER,
        n_lines            INTEGER,
        pct_witness_red    REAL,
        override           TEXT CHECK (override IS NULL OR override IN
                             ('vocal','sparse','no-lyrics')),
        override_note      TEXT,
        pipeline_version   TEXT NOT NULL,
        words_content_hash TEXT CHECK (words_content_hash IS NULL OR
                             length(words_content_hash) = 64),
        computed_at        TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        origin_device_id   TEXT,
        deleted_at         TEXT
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_lyric_verdict_red "
        "ON lyric_verdict(pct_witness_red DESC)"
    ),
)


# ==========================================================================
# DOMAIN: feedback -- one synced row per in-app feedback comment pin
# Legacy source: apps/shared/state/migrations_v12.py (_V12, FBSYNC-01,
# docs/decisions/ADR-0013-feedback-pin-cloudsync.md). Reproduced verbatim
# apart from ``IF NOT EXISTS``, for the adoption reason the lyrics domain
# above spells out.
# ==========================================================================

_FEEDBACK: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS feedback_pins (
        pin_id           TEXT PRIMARY KEY CHECK (length(pin_id) > 0),
        doc              TEXT NOT NULL CHECK (json_valid(doc)),
        updated_at       TEXT NOT NULL,
        origin_device_id TEXT,
        deleted_at       TEXT
    )
    """,
)


# ==========================================================================
# DOMAIN: path_index -- resolver-namespaced disk-truth cache for listing rows
# Legacy source: apps/shared/state/migrations_v18.py (_V18, issue #1037,
# PERF-RB-01). Reproduced verbatim so a database born through this runner can
# serve the same budgeted availability reads as one born through the legacy
# ladder.
# ==========================================================================

_PATH_INDEX: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS path_availability (
        resolver_namespace TEXT NOT NULL,
        logical_path       TEXT NOT NULL,
        materialised_size  INTEGER,
        checked_at         TEXT NOT NULL,
        PRIMARY KEY (resolver_namespace, logical_path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_path_availability_checked "
    "ON path_availability(checked_at)",
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
        modified_at                  TEXT NOT NULL,
        deleted_at                   TEXT
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
    """
    CREATE TABLE IF NOT EXISTS spotify_playlist_links (
        vendor_pl_id          TEXT PRIMARY KEY,
        spotify_playlist_id   TEXT NOT NULL REFERENCES playlists(playlist_id)
                                ON DELETE CASCADE,
        odj_playlist_id       TEXT NOT NULL REFERENCES playlists(playlist_id)
                                ON DELETE CASCADE,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_spotify_playlist_links_odj "
    "ON spotify_playlist_links(odj_playlist_id)",
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
# CACHE DOMAIN: regenerable content caches (audio fingerprints, file digests)
#
# NOT part of the durable ladder. These two tables are pure derived data:
# every row can be recomputed from the audio file it describes, and legacy
# code already treats them as disposable (HashCache DROPs and recreates the
# whole table on a PRAGMA user_version mismatch, O-12). The durable ladder
# must never wipe, so a domain whose legal recovery move IS a wipe cannot
# live in it. They belong in a separate cache DB file; :func:`apply_cache_
# migrations` is their entry point. See REPORT.md, "Durability split".
#
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
    "sync_infra": _SYNC_INFRA,
    "enrollment": _ENROLLMENT,
    "credentials": _CREDENTIALS,
    "analysis": _ANALYSIS,
    "analysis_retention": _ANALYSIS_RETENTION,
    "lyrics": _LYRICS,
    "feedback": _FEEDBACK,
    "path_index": _PATH_INDEX,
    "curation": _CURATION,
    "play_orders": _PLAY_ORDERS,
    "spotify": _SPOTIFY,
    "sets": _SETS,
    "settings": _SETTINGS,
    "dedup": _DEDUP,
    "launcher": _LAUNCHER,
    "sync_write_tokens": _SYNC_WRITE_TOKENS,
}
"""Every DURABLE consolidated domain -> its DDL statements, in creation order.

Regenerable caches are deliberately absent; see :data:`CACHE_DOMAINS`."""

CACHE_DOMAINS: dict[str, tuple[str, ...]] = {
    "caches": _CACHES,
}
"""Regenerable cache domains, owned by a SEPARATE cache DB file.

Split out of :data:`DOMAINS` so the durable ladder's semantics stay honest: a
durable ladder never wipes, and these tables' legal recovery move is exactly a
wipe. Applied by :func:`apply_cache_migrations`, never by
:func:`apply_migrations`."""

LEGACY_SOURCES: dict[str, str] = {
    "state_core": "apps/shared/state/schema.py",
    "sync_infra": "apps/shared/state/schema.py",
    "enrollment": "apps/shared/state/migrations_v9.py",
    "credentials": "apps/shared/state/migrations_v11.py",
    "analysis": "apps/analysis/store.py",
    "native_analysis_v1": "apps/analysis/store.py",
    "analysis_retention": "apps/shared/state/schema.py",
    "lyrics": "apps/shared/state/migrations_v10.py",
    "feedback": "apps/shared/state/migrations_v12.py",
    "path_index": "apps/shared/state/migrations_v18.py",
    "curation": "apps/shared/pairings/schema_sql.py",
    "play_orders": "apps/shared/play_orders/schema.py",
    "spotify": "apps/spotify/state_writer.py",
    "sets": "apps/sets/state.py",
    "settings": "apps/voice/settings.py",
    "dedup": "apps/dedup/schema.py",
    "caches": "apps/shared/fingerprints.py + apps/shared/hashing.py",
    "launcher": "apps/launcher/scripts/bootstrap_db.py",
    "sync_write_tokens": "apps/shared/state/migrations_v21.py",
}
"""Domain -> the legacy file its DDL was lifted from, verbatim.

Covers both :data:`DOMAINS` and :data:`CACHE_DOMAINS`: where a definition came
from is a fact about the definition, not about which file now stores it."""

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
        "users",
        "auth_sessions",
    ),
    "sync_infra": (
        "machines",
        "sync_policies",
        "playlist_pins",
        "sync_state",
        "hub_changelog",
        "local_changelog",
    ),
    "enrollment": (
        "machine_owners",
        "enrollment_grants",
    ),
    "credentials": ("machine_credentials",),
    "analysis": ("analysis", "analysis_events"),
    "native_analysis_v1": (
        "analysis_canonical",
        "analysis_projection",
        "analysis_source_default",
        "analysis_queue_batch",
        "analysis_queue_item",
        "analysis_stale",
    ),
    "analysis_retention": (
        "track_availability",
        "unmatched_source_analysis",
        "track_energy_segments",
        "analysis_field_verification",
    ),
    "lyrics": ("lyric_verdict",),
    "feedback": ("feedback_pins",),
    "path_index": ("path_availability",),
    "curation": ("pairings", "smartlists"),
    "play_orders": ("play_orders", "play_order_entries", "play_orders_schema_meta"),
    "spotify": (
        "spotify_playlist_meta",
        "pending_tracks",
        "spotify_playlist_links",
    ),
    "sets": ("sets", "set_events"),
    "settings": ("settings",),
    "dedup": ("duplicate_clusters", "track_aliases", "tag_provenance"),
    "launcher": ("tracks_fts", "tracks_frecency"),
    "sync_write_tokens": ("sync_write_tokens",),
}
"""Durable domain -> the tables it owns. ``schema_meta`` is excluded on
purpose: it is migration infrastructure, created by the runner, not domain
data. Cache tables live in :data:`CACHE_TABLES`."""

CACHE_TABLES: dict[str, tuple[str, ...]] = {
    "caches": ("fingerprints", "file_hashes"),
}
"""Cache domain -> the tables it owns, in the cache DB file."""

ALL_TABLES: tuple[str, ...] = tuple(
    name for names in TABLES.values() for name in names
)

ALL_CACHE_TABLES: tuple[str, ...] = tuple(
    name for names in CACHE_TABLES.values() for name in names
)


# --- migration ladder -----------------------------------------------------

#: Domains that are NOT part of rung 1 because they arrived later, each as
#: its own rung. Named here rather than inline so the exclusion and the rung
#: that compensates for it cannot drift apart silently.
_POST_V1_DOMAINS: frozenset[str] = frozenset(
    {
        "native_analysis_v1", "enrollment", "lyrics", "credentials", "feedback",
        "path_index", "sync_write_tokens",
    }
)

_V1: list[str] = [
    stmt for name, domain in DOMAINS.items()
    if name not in _POST_V1_DOMAINS
    for stmt in domain
]
"""Consolidated 0 -> 1: create everything that existed at v1. Fresh-DB path."""

_V2: list[str] = list(_NATIVE_ANALYSIS_V1)
"""1 -> 2: native-analysis v1's canonical pointer, projection and source default.

A NEW rung rather than an append to _V1. `apply_migrations` returns at
`current >= SCHEMA_VERSION`, so a state.db already stamped at v1 -- which is
every existing install -- would never have run an appended statement, and the
fresh-DB tests would have passed anyway. Reproduced Wed 9 Sep 2026: dropping
the two tables from a stamped database and re-running the ladder left them
absent. Every statement is `IF NOT EXISTS`, so the rung is also safe on a
database that already has them."""

_V3: list[str] = list(_ENROLLMENT)
"""2 -> 3: machine ownership and enrollment grants (legacy ladder v9).

A new rung for the reason _V2 spells out: an install already stamped at v2
never re-runs _V1, so an enrollment table appended there would exist only on
databases born after this commit -- and the fresh-DB tests would have passed
anyway. The legacy ladder makes the same move at its own v9."""

_V4: list[str] = list(_LYRICS)
"""3 -> 4: the karaoke lyrics verdict row and its triage index.

Its own rung for the reason _V2 and _V3 spell out. Mirrors legacy ``_V10``
(apps/shared/state/migrations_v10.py); the legacy sync_policies rebuild that
ships in the same legacy step is NOT mirrored as a rebuild, because this
module declares the END shape directly -- its widened ``asset_kind`` CHECK is
already in ``_SYNC_INFRA``. ``LEGACY_SHARED_STATE_VERSION`` deliberately stays
where scripts/sync_drift_rules.MIRROR_VERSION_DEBT pins it; REPORT.md O-16
records what that costs."""

_V5: list[str] = list(_CREDENTIALS)
"""4 -> 5: the per-machine sync credential (legacy ladder v11).

Its own rung for the reason _V2 and _V3 spell out: an install already
stamped at v4 never re-runs an earlier rung."""

_V6: list[str] = list(_FEEDBACK)
"""5 -> 6: the synced feedback pin row (legacy ladder v12, FBSYNC-01).

Its own rung for the reason _V2 and _V3 spell out. ``LEGACY_SHARED_STATE_VERSION``
stays where scripts/sync_drift_rules.MIRROR_VERSION_DEBT pins it."""

_V7: list[str] = list(_PATH_INDEX)
"""6 -> 7: the persisted path availability index (legacy ladder v18, PERF-RB-01).

Its own rung for the reason _V2 and _V3 spell out: an install already
stamped at v6 never re-runs an earlier rung."""

_V8: list[str] = [
    "ALTER TABLE tracks ADD COLUMN audio_hash TEXT",
    "CREATE INDEX IF NOT EXISTS idx_tracks_audio_hash ON tracks(audio_hash) "
    "WHERE audio_hash IS NOT NULL",
]
"""7 -> 8: tag-independent audio identity on tracks (legacy ladder v19, issue #3864).

Its own rung for the reason _V2 and _V3 spell out: an install already
stamped at v7 never re-runs an earlier rung."""

_V9: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_tracks_content_hash ON tracks(content_hash) "
    "WHERE content_hash IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_tracks_isrc_upper ON tracks(upper(isrc))",
]
"""8 -> 9: indexed CloudSync track identity lookups (legacy ladder v20, issue #4397).

Its own rung for the reason _V2 and _V3 spell out: an install already
stamped at v8 never re-runs an earlier rung."""

_V10: list[str] = list(_SYNC_WRITE_TOKENS)
"""9 -> 10: sync_write_tokens, the CloudSync digest gate's per-table write
token (legacy ladder v21, issue #4396).

Its own rung for the reason _V2 and _V3 spell out: an install already
stamped at v9 never re-runs an earlier rung."""

_V11: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_playlist_memberships_live_order "
    "ON playlist_memberships("
    "playlist_id, COALESCE(order_key, printf('%08d', position)), position"
    ") WHERE deleted_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_playlist_memberships_live_stable_id "
    "ON playlist_memberships(playlist_id, stable_id) WHERE deleted_at IS NULL",
]
"""10 -> 11: bounded playlist membership reads (legacy ladder v22, issue #3963).

Its own rung for the reason _V2 and _V3 spell out: an install already
stamped at v10 never re-runs an earlier rung."""

_V12: list[str] = [
    "ALTER TABLE tracks ADD COLUMN restored_at TEXT",
    "ALTER TABLE tracks ADD COLUMN deleted_reason TEXT",
]
"""11 -> 12: the explicit-restore stamp on tracks (legacy ladder v23, LIBM-140).

Its own rung for the reason _V2 and _V3 spell out: an install already
stamped at v11 never re-runs an earlier rung."""

MIGRATIONS: list[list[str]] = [
    _V1, _V2, _V3, _V4, _V5, _V6, _V7, _V8, _V9, _V10, _V11, _V12,
]

ALL_DDL: list[str] = [stmt for rung in MIGRATIONS for stmt in rung]
"""Every rung, flattened. What both the fresh path and adoption execute.

The runner does not replay rungs one at a time: it runs the whole ladder and
stamps the target version, and every statement is ``IF NOT EXISTS``, so a
database that already has an object is a no-op rather than an error. The
rungs still exist as separate lists because SCHEMA_VERSION is what decides
whether an ALREADY-STAMPED database is brought forward at all -- adding a
table without a new rung and a version bump means no existing install ever
gets it, which is exactly what happened here before v2."""

# Data-bearing statements the legacy ladder carried alongside its DDL. They
# are re-run on adoption because a DB adopted mid-ladder may have the table
# but not the rows. Idempotent by construction (INSERT OR IGNORE), so they
# are safe on a fully-migrated DB and no-ops on a fresh one.
# Source: apps/shared/state/schema.py _V4.
_ADOPTION_BACKFILL: tuple[str, ...] = (
    # OR IGNORE alone stopped being a dedupe at v6: the unique index gained
    # machine_id, which is NULL until the post-migration stamping hook runs,
    # and SQLite treats NULLs as distinct -- so OR IGNORE would re-insert
    # every already-backfilled row. The NOT EXISTS guard dedupes on the
    # logical identity irrespective of machine_id.
    """
    INSERT OR IGNORE INTO track_locations(
        stable_id, kind, role, file_path, created_at, updated_at
    )
    SELECT stable_id, 'local', 'primary', file_path, updated_at, updated_at
    FROM tracks
    WHERE file_path IS NOT NULL AND file_path != ''
      AND NOT EXISTS (
        SELECT 1 FROM track_locations tl
        WHERE tl.stable_id = tracks.stable_id
          AND tl.kind = 'local'
          AND tl.file_path = tracks.file_path
      )
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
    """Create the migration bookkeeping tables with the legacy shape, byte-for-byte.

    ``schema_meta`` is the same DDL as apps/shared/state/schema.py._ensure_meta
    and ``schema_meta_markers`` the same as apps/shared/state/migrations_v16
    (the one-shot repair markers of #3165), so a DB that has already been
    through the legacy runner sees an exact no-op. Both are infrastructure,
    hence :data:`apps.shared.state.schema.INFRASTRUCTURE_TABLES` and not
    :data:`TABLES`.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta_markers (
            marker     TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
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
    """Names of every table (incl. virtual) AND view already in ``conn``.

    Views are counted because SQLite shares one namespace between them and
    tables: with a view called ``pairings`` in the file, ``CREATE TABLE IF NOT
    EXISTS pairings`` is a silent no-op. Filtering to ``type = 'table'`` made
    that collision invisible to both the missing-object census and the
    adopt-or-create decision. Real state DBs do carry views (``tracks_available``
    and friends, see REPORT.md), so this is a live collision class.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    return {str(r[0]) for r in rows}


def normalize_object_sql(sql: str) -> str:
    """Strip the ``sqlite_master`` noise that carries no schema meaning.

    * ``IF NOT EXISTS`` -- a creation-time flag, not part of the shape.
    * identifier double-quotes -- SQLite re-emits a renamed table's DDL with
      the name quoted (``track_fields`` reaches its v3 shape via ``ALTER TABLE
      track_fields_v3 RENAME TO ...``), an artifact of HOW a DB got there
      rather than of what it holds.
    * whitespace runs and the trailing semicolon -- indentation differs
      between the legacy files this module consolidates.

    Shared by the pre-adoption shape audit and the legacy-equivalence test, so
    "same shape" means one thing in this codebase rather than two.
    """
    out = _IF_NOT_EXISTS_RE.sub("", sql)
    out = out.replace('"', "")
    out = _WHITESPACE_RE.sub(" ", out)
    return out.strip().rstrip(";").strip()


@cache
def _reference_objects() -> dict[str, tuple[str, str]]:
    """``name -> (type, normalised sql)`` for everything the ladder creates.

    Built by running the ladder into an in-memory DB and reading back what
    SQLite actually stored, so the expectation is the real shape rather than a
    hand-kept transcription that can drift from the DDL beside it.

    FTS5 shadow tables (``tracks_fts_data`` and friends) are excluded: they
    are implementation detail of the one ``CREATE VIRTUAL TABLE`` that spawns
    them and carry no independent contract.
    """
    reference = sqlite3.connect(":memory:")
    try:
        # ALL_DDL, not _V1. The audit reference has to be built from the SAME
        # statement set creation runs, or a rung added after v1 is never
        # audited: `CREATE TABLE IF NOT EXISTS` would preserve a malformed
        # pre-existing object and the file would still be stamped at the new
        # version (Codex P2, PR #1549).
        for statement in ALL_DDL:
            reference.execute(statement)
        shadow_prefixes = tuple(
            f"{row[0]}_"
            for row in reference.execute(
                "SELECT name FROM sqlite_master WHERE sql LIKE '%USING fts5%'"
            ).fetchall()
        )
        objects: dict[str, tuple[str, str]] = {}
        for name, obj_type, sql in reference.execute(
            "SELECT name, type, sql FROM sqlite_master WHERE sql IS NOT NULL"
        ).fetchall():
            text = str(name)
            if text.startswith("sqlite_") or text.startswith(shadow_prefixes):
                continue
            objects[text] = (str(obj_type), normalize_object_sql(str(sql)))
        return objects
    finally:
        reference.close()


def _audit_existing_shapes(conn: sqlite3.Connection) -> None:
    """Refuse when a pre-existing object does not MATCH what the ladder builds.

    Every ladder statement is ``IF NOT EXISTS``, which makes name-presence and
    shape-presence two different things: a pre-existing object with the right
    name and the wrong shape (or the wrong TYPE -- a view where a table is
    expected) silently absorbs the create and the runner reports success.
    Checking names only, as the pre-adoption audit did, cannot see any of that.

    Compares normalised SQL for every object the ladder would create that the
    file already has. Objects the ladder does not own are none of its business
    and are left alone.
    """
    reference = _reference_objects()
    live = {
        str(name): (str(obj_type), normalize_object_sql(str(sql)))
        for name, obj_type, sql in conn.execute(
            "SELECT name, type, sql FROM sqlite_master WHERE sql IS NOT NULL"
        ).fetchall()
    }
    for name in sorted(reference):
        if name not in live:
            continue
        expected_type, expected_sql = reference[name]
        found_type, found_sql = live[name]
        if (found_type, found_sql) == (expected_type, expected_sql):
            continue
        raise SchemaAdoptionError(
            f"pre-existing {found_type} {name!r} does not match the shape this "
            f"ladder creates. Every ladder statement is IF NOT EXISTS, so "
            f"adoption would silently leave the wrong shape in place and "
            f"report success.\n"
            f"  expected ({expected_type}): {expected_sql}\n"
            f"  found    ({found_type}): {found_sql}\n"
            f"Reconcile {name!r} by hand, or run the legacy bootstrap that "
            "owns it, before adopting. This runner never overwrites an object "
            "it did not create."
        )


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


def _track_locations_has_v6_shape(conn: sqlite3.Connection) -> bool:
    """True when track_locations carries the v6 rebuild's shape.

    Legacy v6 REBUILDS the table: integer autoincrement id becomes
    location_id TEXT, and machine_id joins per specs/design_decision_08.md.
    Adoption only creates missing objects, so a pre-v6 shape must be refused,
    exactly like the v2/v3 rebuilds above.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(track_locations)")}
    return "location_id" in cols and "machine_id" in cols


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
    (
        "track_locations",
        _track_locations_has_v6_shape,
        "existing track_locations table predates the v6 rebuild "
        "(integer id primary key, no location_id/machine_id)",
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


_ADDED_TRACK_COLUMNS: tuple[str, ...] = ("audio_hash", "restored_at", "deleted_reason")
"""Columns a rung adds to ``tracks`` with a bare ``ALTER TABLE``."""


def _adoption_ddl(conn: sqlite3.Connection) -> list[str]:
    """Return ladder DDL with every already-present column add removed.

    ``ALTER TABLE ... ADD COLUMN`` has no ``IF NOT EXISTS``, so a database the
    legacy ladder already brought forward must skip the rungs that add a
    column it holds (v8 ``audio_hash``, v12 ``restored_at`` and ``deleted_reason``).
    """
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tracks)")}
    present = {
        f"ALTER TABLE tracks ADD COLUMN {column} TEXT"
        for column in _ADDED_TRACK_COLUMNS
        if column in columns
    }
    return [stmt for stmt in ALL_DDL if stmt not in present]


def _rollback_without_masking(
    conn: sqlite3.Connection, original: BaseException
) -> None:
    """Undo a partial migration without ever displacing ``original``.

    SQLite auto-rolls-back on some failures (SQLITE_FULL, SQLITE_IOERR). After
    one of those, an unconditional ``ROLLBACK`` raises "cannot rollback - no
    transaction is active", and that secondary error propagates INSTEAD of the
    real one -- so a full disk gets reported as a transaction-state complaint
    and the operator debugs the wrong thing.

    Two guards: skip the rollback when no transaction is live, and if it fails
    regardless (a connection broken worse than that), attach the failure to
    ``original`` as a note. Noting rather than raising keeps the cause intact;
    noting rather than passing keeps a possibly half-migrated file visible.
    """
    if not conn.in_transaction:
        return
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error as rollback_failure:
        original.add_note(
            f"rollback after the above failure ALSO failed: {rollback_failure!r}. "
            "The database may hold a partially applied migration."
        )


def _adopt(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Bring a pre-existing DB up to the consolidated shape.

    Creates only what is missing (every statement is already IF NOT EXISTS,
    so this is belt-and-braces: the returned tuple is the audit trail of what
    the file was actually short of), replays the data-bearing backfills, and
    leaves the ADOPTION marker to the caller. Returns the tables that were
    missing before the call.

    Assumes the caller has already run the pre-flight gate
    (:func:`_assert_adoptable` + :func:`_audit_existing_shapes`).
    """
    was_missing = missing_tables(conn)
    _create_all(conn, _adoption_ddl(conn))
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
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
    _ensure_meta(conn)
    current = consolidated_version(conn)
    if current >= SCHEMA_VERSION:
        return current

    adopting = bool(_legacy_version(conn)) or bool(
        existing_objects(conn) & set(ALL_TABLES)
    )

    # Pre-flight gate, outside the transaction: nothing is written until every
    # pre-existing object has been shown to be one this ladder can live with.
    # Rebuild probes run first because they carry the actionable remediation
    # ("run the legacy ladder"); the shape audit is the general net behind them
    # and covers the fresh path too, where a stray index or view could still
    # absorb a create.
    if adopting:
        _assert_adoptable(conn)
    migrate_smartlists_deleted_at(conn)
    _audit_existing_shapes(conn)

    # IMMEDIATE, not the default DEFERRED: this runner reads (the census and
    # the audit above, plus missing_tables inside _adopt) and then writes. A
    # deferred transaction takes its read snapshot first and only requests the
    # write lock at the first CREATE; if a competing writer takes RESERVED in
    # that window, SQLite returns SQLITE_BUSY on the upgrade and will NOT
    # retry it whatever busy_timeout says, because retrying an upgrade can
    # deadlock. Taking the write lock up front makes the wait honour the
    # timeout instead of failing outright.
    conn.execute("BEGIN IMMEDIATE")
    try:
        if adopting:
            _adopt(conn)
            _record(conn, ADOPTION_VERSION)
        else:
            _create_all(conn, ALL_DDL)
        _stamp_legacy_counters(conn)
        _record(conn, VERSION_OFFSET + SCHEMA_VERSION)
        conn.execute("COMMIT")
    except Exception as original:
        _rollback_without_masking(conn, original)
        raise

    return consolidated_version(conn)


def apply_cache_migrations(conn: sqlite3.Connection) -> None:
    """Create the regenerable content caches. Idempotent.

    ``conn`` is meant to be a SEPARATE cache DB file (``cache.db``), not the
    engine state DB. The split is about semantics, not tidiness: every row
    here is derived from an audio file and can be recomputed, so wiping the
    file is a legal recovery move. The durable ladder must never wipe, and
    keeping a wipe-able domain inside it would make that promise a lie -- the
    legacy ``HashCache`` already DROPs and recreates its whole table on a
    version mismatch (REPORT.md O-12).

    No version counter: this is one flat DDL set, and a counter on a file
    whose recovery move is deletion would be ceremony. If a shape ever needs
    to change, the cache is rebuilt.

    NOTE: nothing in the engine calls this yet. Wiring the cache DB into the
    daemon is a later tranche; this function exists so ownership of the DDL is
    unambiguous now, and so :func:`apply_migrations` can honestly claim the
    durable ladder holds only durable judgment.
    """
    for statements in CACHE_DOMAINS.values():
        for statement in statements:
            conn.execute(statement)


def was_adopted(conn: sqlite3.Connection) -> bool:
    """True iff this DB reached the consolidated schema by adoption."""
    row = conn.execute(
        "SELECT 1 FROM schema_meta WHERE version = ?", (ADOPTION_VERSION,)
    ).fetchone()
    return row is not None


__all__ = [
    "ADOPTION_VERSION",
    "ALL_CACHE_TABLES",
    "ALL_DDL",
    "ALL_TABLES",
    "BUSY_TIMEOUT_MS",
    "CACHE_DOMAINS",
    "CACHE_TABLES",
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
    "apply_cache_migrations",
    "apply_migrations",
    "consolidated_version",
    "ensure_vendor_sidecar_tables",
    "existing_objects",
    "missing_tables",
    "normalize_object_sql",
    "was_adopted",
]
