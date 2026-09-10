"""Migration ladder (v6-v8) for the shared state schema.

Continuation of :mod:`apps.shared.state.migrations` (issue #1583): v1-v5 live
there, v6-v8 live here. Split purely because the combined ladder exceeds the
600-line file-size gate; there is no semantic boundary at v5/v6, only a size
one. :data:`apps.shared.state.schema.MIGRATIONS` assembles both halves in
order and :func:`apps.shared.state.schema.apply_migrations` is the runner --
both stay in ``schema.py`` along with the drift-tripwire table/view tuples.
"""
from __future__ import annotations

# --- migration 6 -> 7: sync-safe schema (CLOUDSYNC) ------------------------
# Contract: specs/design_decision_05.md, rationale specs/design_decision_04.md.
# Three facts blocked hub sync: track_locations had an INTEGER PRIMARY KEY
# AUTOINCREMENT that collides across machines, no synced table carried
# updated_at / origin_device_id / deleted_at, and there were no machines or
# policy tables. Pure SQL, no Python, so it rides the existing MIGRATIONS
# machinery.
#
# Amended IN PLACE for round 2 by specs/design_decision_08.md points 1 and 2
# (this migration had never touched a real database). ADR 05 reserved v6 for
# CLOUDSYNC and v7 for auth, but the auth branch (Google sign-in) merged to
# main first and took v6, so CLOUDSYNC yields and renumbers to v7: this list
# sits ABOVE main's auth _V6 in the ladder. Same statements, one step higher.
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
# 5. ``sync_state.peer_generation`` is the last generation token a peer
#    reported (apps/sync_hub/generation.py, round 2 finding N6). Restore
#    detection keys on that token changing rather than on the peer's
#    ``MAX(seq)`` going backwards, so pruning a changelog no longer looks
#    like a Litestream restore and no longer makes every spoke re-offer its
#    whole library. NULL until the first sync against that peer completes.
_V7: list[str] = [
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
        peer            TEXT PRIMARY KEY,
        last_push_seq   INTEGER NOT NULL DEFAULT 0,
        last_pull_seq   INTEGER NOT NULL DEFAULT 0,
        last_sync_at    TEXT,
        peer_generation TEXT
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

# --- migration 5 -> 6: Google sign-in (users + auth_sessions) --------------
# The webui gained a user bauble that signs in with Google (openid/email/
# profile only). Two tables, both keyed off Google's ``sub`` claim -- the
# only identifier Google guarantees is stable and never reused. Email is
# NOT the key: Google account emails can change.
#
# ``auth_sessions.session_token_sha256`` stores a hash, never the bearer
# value the browser holds, so a stolen DB cannot be replayed as a cookie.
# ``refresh_token`` is the Google grant that produced the session; it stays
# server-side and is never exposed over the API.
#
# Numbered 6, not 4: this landed on a branch cut before main's
# track_locations work, and both claimed _V4 independently. A DB that had
# already applied main's 4 and 5 would never have run these statements, so
# the auth tables move to their own step rather than sharing a number.
_V6: list[str] = [
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


# --- migration 7 -> 8: analysis retention for audio we do not have --------
# the maintainer, Tue 28 Jul 2026: "can we still grab the analyses of the files we don't
# have but mark them file missing? maybe in a different table to prevent
# confusion later".
#
# Numbered 8, not 4: this branch was cut before track_locations (v4/v5),
# Google sign-in (v6) and CLOUDSYNC (v7) landed on main, and all four
# claimed v4 independently. Same renumbering rationale as v6/v7 above --
# this migration lands on top of the ladder instead of colliding with it.
#
# Two DISTINCT cases, deliberately NOT collapsed into one table:
#
# (a) A track that HAS a ``tracks`` row but whose audio is absent on disk
#     (7,166 of 8,355 rows measured Tue 28 Jul 2026). Its analysis stays in
#     ``track_fields`` -- duplicating it into an orphan table would create a
#     migration that rots the moment the file is re-acquired. Availability is
#     modelled as an explicit DIMENSION instead: ``track_availability``, one
#     row per stable_id, plus views so the safe default is the easy one.
#
# (b) An analysed SOURCE row that matches no ``tracks`` row at all (2,603 of
#     MIK's 7,026 songs). There is no stable_id to hang a ``track_fields`` row
#     on, and inventing one would fabricate identity. It gets its own staging
#     table, ``unmatched_source_analysis``, with a nullable
#     ``promoted_stable_id`` recording the later match. See
#     ``docs/analysis-retention.md`` for the promotion path.
#     ``unmatched_reason`` records WHY no stable_id was assigned: no candidate
#     at all, several equally good candidates, or a candidate that a
#     better-tier source row won. All three mean the same thing operationally
#     -- we do not know which track this analysis belongs to -- which is
#     exactly why it must not sit in ``track_fields``.
#
# ``track_energy_segments`` is the time-series destination for MIK's
# ZENERGYSEGMENT rows, in MILLISECONDS per the repo convention
# (docs/terminology-reference/time-series-vs-scalar.md). ``track_fields`` is
# one row per (stable_id, field_name) and cannot hold a series.
_V8: list[str] = [
    # (a) availability as a dimension, not a duplicated analysis table.
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
    # Safe-default views. An aggregate written against ``tracks`` silently
    # includes unplayable rows; one written against ``tracks_available``
    # cannot. Rows with NO availability row yet are UNKNOWN and are excluded
    # from ``tracks_available`` on purpose (absence of evidence is not
    # evidence of presence).
    #
    # ``deleted_at IS NULL`` is required here, not implied by the
    # ``track_availability`` FK's ``ON DELETE CASCADE``: a soft delete only
    # sets ``tracks.deleted_at``, it never issues a physical ``DELETE``, so
    # the cascade never fires and a tombstoned track's ``track_availability``
    # row (and a tombstoned track_fields row) survives it. Without this
    # predicate these safe-default views resurrect synced ghosts and inflate
    # aggregates built against them.
    """
    CREATE VIEW IF NOT EXISTS tracks_available AS
    SELECT t.*
    FROM tracks t
    JOIN track_availability a ON a.stable_id = t.stable_id
    WHERE a.state = 'present' AND t.deleted_at IS NULL
    """,
    """
    CREATE VIEW IF NOT EXISTS tracks_unavailable AS
    SELECT t.*, a.state AS availability_state, a.checked_at AS availability_checked_at
    FROM tracks t
    JOIN track_availability a ON a.stable_id = t.stable_id
    WHERE a.state <> 'present' AND t.deleted_at IS NULL
    """,
    """
    CREATE VIEW IF NOT EXISTS track_fields_available AS
    SELECT f.*
    FROM track_fields f
    JOIN track_availability a ON a.stable_id = f.stable_id
    JOIN tracks t ON t.stable_id = f.stable_id
    WHERE a.state = 'present' AND f.deleted_at IS NULL AND t.deleted_at IS NULL
    """,
    # (b) analysis with no stable_id to hang it on. Staging, not truth:
    # ``promoted_stable_id`` is the one-way door into ``track_fields``.
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
    # Energy time series, milliseconds, time-indexed. Boundaries are rounded,
    # never a start and a duration independently: see
    # apps/mik/mikdb.py::_segment_rows_by_song. ``start_clamped`` marks the 77
    # rows whose source start time was negative float dust, so a reader can
    # tell a genuine 0 start from a clamped one.
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
    # HOW each field was verified, stored next to the values it produced.
    # Cross-source agreement and a one-sided single-source probe are not the
    # same strength of evidence, and a bare "passed" in a log line does not
    # survive six months. Written by apps.mik.load.record_verification from
    # apps.shared.equivalence.EquivalenceGate.provenance_rows, so a reader
    # cannot mistake one basis for the other. ``overridden`` records that a
    # human forced the write past a non-passing verdict.
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
]
