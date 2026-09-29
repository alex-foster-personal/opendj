"""SQLite schema + migrations for the shared state layer.

Schema is derivative: can be wiped and rebuilt from vendor DBs at any time.
Migrations are idempotent-at-the-statement-level; ``apply_migrations`` uses
a ``schema_meta`` table to skip already-applied versions.

Versioning: ``SCHEMA_VERSION`` is the target version. ``MIGRATIONS`` is a
list where index ``i`` is the SQL to take the schema from version ``i`` to
``i+1``. A fresh DB runs the full list. The ladder itself (``_V1``..``_V12``)
lives in :mod:`apps.shared.state.migrations` (v1-v5) and
:mod:`apps.shared.state.migrations_v6_v8` (v6-v8) and
:mod:`apps.shared.state.migrations_v9` (v9) and
:mod:`apps.shared.state.migrations_v10` (v10) and
:mod:`apps.shared.state.migrations_v11` (v11) and
:mod:`apps.shared.state.migrations_v12` (v12) and
:mod:`apps.shared.state.migrations_v13` (v13) and
:mod:`apps.shared.state.migrations_v14` (v14) and
:mod:`apps.shared.state.migrations_v15` (v15) and
:mod:`apps.shared.state.migrations_v16` (v16) -- split across sibling modules
(issue #1583) because the combined ladder alone exceeds the 600-line
file-size gate. This module keeps the runner and the
drift-tripwire table/view tuples below.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from .migrations import _V1, _V2, _V3, _V4, _V5
from .migrations_v6_v8 import _V6, _V7, _V8
from .migrations_v9 import _V9
from .migrations_v10 import _V10
from .migrations_v11 import _V11
from .migrations_v12 import _V12
from .migrations_v13 import _V13
from .migrations_v14 import _V14
from .migrations_v15 import _V15, backfill_track_fields_stamps
from .migrations_v16 import _V16
from .migrations_v17 import _V17, repair_hub_changelog_stamps
from .migrations_v18 import _V18
from .migrations_v19 import _V19
from .migrations_v20 import _V20

SCHEMA_VERSION: int = 20


# Each element is the set of SQL statements that take schema from N to N+1.
# MIGRATIONS[0] runs when going from v0 (empty) to v1.
MIGRATIONS: list[list[str]] = [
    _V1,
    _V2,
    _V3,
    _V4,
    _V5,
    _V6,
    _V7,
    _V8,
    _V9,
    _V10,
    _V11,
    _V12,
    _V13,
    _V14,
    _V15,
    _V16,
    _V17,
    _V18,
    _V19,
    _V20,
]


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

    A db already at :data:`SCHEMA_VERSION` takes no write lock: the version
    is read outside any transaction, which WAL permits while another process
    holds a long write. Only a stale db enters the ``BEGIN IMMEDIATE`` loop,
    and there the version is re-read under the lock so concurrent boots
    observe the winner's ``schema_meta`` row before choosing DDL (issue #791).
    """
    _ensure_meta(conn)

    while _current_version(conn) < SCHEMA_VERSION:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _current_version(conn)
            if current >= SCHEMA_VERSION:
                conn.execute("COMMIT")
                break

            step_idx = current
            statements = MIGRATIONS[step_idx]
            target_version = step_idx + 1
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
                (target_version, datetime.now(UTC).isoformat()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    if _current_version(conn) >= 16:
        backfill_track_fields_stamps(conn)
    if _current_version(conn) >= 17:
        repair_hub_changelog_stamps(conn)

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
    # v6 (auth, Google sign-in) -- landed on main first, took v6
    "users",
    "auth_sessions",
    # v7 (CLOUDSYNC, specs/design_decision_05.md) -- yielded to v7
    "machines",
    "sync_policies",
    "playlist_pins",
    "sync_state",
    "hub_changelog",
    "local_changelog",
    # v8 (analysis retention for audio we do not have)
    "track_availability",
    # v18 (issue #1037 path index for bounded listing stat budgets)
    "path_availability",
    "unmatched_source_analysis",
    "track_energy_segments",
    "analysis_field_verification",
    # v9 (MACHINE ENROLLMENT, specs/design_decision_12.md) -- hub-authoritative
    # and OUTSIDE the sync set, so neither appears in protocol.SYNC_TABLES
    "machine_owners",
    "enrollment_grants",
    # v10 (karaoke lyrics verdict, specs/karaoke-lyrics-operational-plan.md D13.1)
    "lyric_verdict",
    # v11 (per-machine sync credential, ADR 12 amendment) -- hub-local, unsynced
    "machine_credentials",
    # v12 (FBSYNC-01, docs/decisions/ADR-0013-feedback-pin-cloudsync.md)
    "feedback_pins",
    # v20 (issue #4396) -- machine-local digest gate, never synced
    "sync_write_tokens",
)
"""Domain tables created by :data:`MIGRATIONS`. ``schema_meta`` is
intentionally excluded -- it is infrastructure, not domain data. Four of
them arrived in v8, two in v9, ``lyric_verdict`` in v10,
``machine_credentials`` in v11 and ``feedback_pins`` in v12."""

VIEWS: tuple[str, ...] = (
    "tracks_available",
    "tracks_unavailable",
    "track_fields_available",
)
"""Safe-default views created by migration v8. Query these, not ``tracks``,
whenever an aggregate must not silently count unplayable rows."""

AVAILABILITY_STATES: tuple[str, ...] = (
    "present",
    "absent",
    "awaiting_volume",
    "streaming",
)
"""Mirror of the ``track_availability.state`` CHECK constraint. Kept in sync
by ``tests/shared/state/test_schema.py``."""

LYRIC_VERDICTS: tuple[str, ...] = (
    "vocal",
    "sparse",
    "no-lyrics",
    "unknown",
)
"""Mirror of the ``lyric_verdict.verdict`` CHECK constraint (v10). Kept in
sync by ``tests/shared/state/test_schema_v10.py``, which inserts every member.

Deliberately NOT in a module named ``verdict.py``: ``apps/lyrics/verdict.py``
is an unrelated classifier with its own vocabulary, and two same-named
vocabularies one import apart is how a wrong enum ends up in a CHECK."""

LYRIC_OVERRIDES: tuple[str, ...] = (
    "vocal",
    "sparse",
    "no-lyrics",
)
"""Mirror of the ``lyric_verdict.override`` CHECK constraint (v10): the human
verdicts a person can force. ``'unknown'`` is absent on purpose -- overriding
a computed verdict back to "we do not know" is not a judgment, it is a
delete, and a delete of an override is spelled NULL."""


FOREIGN_AUTHORITY_TABLES: tuple[str, ...] = (
    # apps/shared/pairings/schema_sql.py :: ensure_phase08_tables
    "pairings",
    "smartlists",
    # apps/shared/pairings/schema_sql.py :: apply_pairing_capture_migrations
    # -- the SECOND ladder in that same module, with its own version
    # counter, applied lazily by apps/webui/server/routes/pairing_capture.py
    # against the daemon's writable state.db (PairingCaptureRepo defaults to
    # ensure_schema=True). Undeclared here until Wed 9 Sep 2026, when
    # scripts/sync_drift_lint.py D-08 named all three at once: the tripwire
    # in tests/shared/state/test_schema_v6.py hand-copied the authority list
    # and the copy only ever had the first entry point, so no inventory and
    # no test had heard of these tables.
    "pairing_sync_snapshots",
    "pairing_alignments",
    "pairing_capture_schema_meta",
    # apps/shared/play_orders/schema.py (private version counter)
    "play_orders",
    "play_order_entries",
    "play_orders_schema_meta",
    # apps/shared/playlist_sets/schema.py (SET-05 private version counter)
    "playlist_sets",
    "playlist_set_entries",
    "playlist_set_runs",
    "playlist_sets_schema_meta",
    # apps/launcher/scripts/bootstrap_db.py
    "tracks_fts",
    "tracks_fts_config",
    "tracks_fts_content",
    "tracks_fts_data",
    "tracks_fts_docsize",
    "tracks_fts_idx",
    "tracks_frecency",
    # apps/launcher/src-tauri/src/state.rs :: ensure_launcher_meta -- the one
    # authority that is not Python. get_db_path prefers <repo>/data/state/
    # state.db whenever it exists, and every meta_get/meta_set runs
    # CREATE TABLE IF NOT EXISTS first, so a single launcher start creates
    # this table in the live shared file. Undeclared and undocumented until
    # Wed 9 Sep 2026: every derivation of the authority list had been done by
    # reading *.py, so no inventory, no test and no docs run had ever heard
    # of it, and regenerating AGENTS.md against a launcher-touched state.db
    # raised MissingColumnDocsError.
    "launcher_meta",
    # apps/sync_hub/engine_identity_map.py :: ensure_identity_remap_table --
    # spoke-side bookkeeping for content-identity collapses that must outlive
    # one hub_apply batch. Not in the sync set itself (its own docstring says
    # so): it carries no updated_at/origin_device_id/deleted_at, and
    # apps/sync_hub/client.py runs it directly against the shared state.db
    # connection returned by state_db.open_rw.
    "sync_identity_remap",
)
"""Tables this module does NOT create but that legitimately live in the same
file, written by the other three schema authorities (spec section 1.3 of
``specs/cloudsync-spec.md``). Declared so the drift tripwire can pass on a
fully-provisioned DB while still failing on a genuinely undeclared table.
D2 folds these into :data:`MIGRATIONS` in a later round; until then this
tuple is the honest inventory, not an aspiration."""


INFRASTRUCTURE_TABLES: tuple[str, ...] = ("schema_meta", "schema_meta_markers")
"""Migration bookkeeping owned by this module."""


ALL_KNOWN_TABLES: frozenset[str] = frozenset(
    TABLES + FOREIGN_AUTHORITY_TABLES + INFRASTRUCTURE_TABLES
)
"""Every non-``sqlite_*`` table name expected in a fully-provisioned state DB.
Anything in ``sqlite_master`` outside this set is undeclared drift."""
