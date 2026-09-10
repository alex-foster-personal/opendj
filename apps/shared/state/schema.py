"""SQLite schema + migrations for the shared state layer.

Schema is derivative: can be wiped and rebuilt from vendor DBs at any time.
Migrations are idempotent-at-the-statement-level; ``apply_migrations`` uses
a ``schema_meta`` table to skip already-applied versions.

Versioning: ``SCHEMA_VERSION`` is the target version. ``MIGRATIONS`` is a
list where index ``i`` is the SQL to take the schema from version ``i`` to
``i+1``. A fresh DB runs the full list. The ladder itself (``_V1``..``_V9``)
lives in :mod:`apps.shared.state.migrations` (v1-v5),
:mod:`apps.shared.state.migrations_v6_v8` (v6-v8) and
:mod:`apps.shared.state.migrations_v9` (v9) -- split across three sibling
modules (issue #1583) because the combined ladder alone exceeds the
600-line file-size gate. This module keeps the runner and the
drift-tripwire table/view tuples below.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from .migrations import _V1, _V2, _V3, _V4, _V5
from .migrations_v6_v8 import _V6, _V7, _V8
from .migrations_v9 import _V9

SCHEMA_VERSION: int = 9


# Each element is the set of SQL statements that take schema from N to N+1.
# MIGRATIONS[0] runs when going from v0 (empty) to v1.
MIGRATIONS: list[list[str]] = [_V1, _V2, _V3, _V4, _V5, _V6, _V7, _V8, _V9]


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
                (target_version, datetime.now(UTC).isoformat()),
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
    "unmatched_source_analysis",
    "track_energy_segments",
    "analysis_field_verification",
    # v9 (karaoke lyrics verdict, specs/karaoke-lyrics-operational-plan.md D13.1)
    "lyric_verdict",
)
"""Domain tables created by :data:`MIGRATIONS`. ``schema_meta`` is
intentionally excluded -- it is infrastructure, not domain data. The four
before the last arrived in v8, ``lyric_verdict`` in v9."""

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
"""Mirror of the ``lyric_verdict.verdict`` CHECK constraint (v9). Kept in
sync by ``tests/shared/state/test_schema_v9.py``, which inserts every member.

Deliberately NOT in a module named ``verdict.py``: ``apps/lyrics/verdict.py``
is an unrelated classifier with its own vocabulary, and two same-named
vocabularies one import apart is how a wrong enum ends up in a CHECK."""

LYRIC_OVERRIDES: tuple[str, ...] = (
    "vocal",
    "sparse",
    "no-lyrics",
)
"""Mirror of the ``lyric_verdict.override`` CHECK constraint (v9): the human
verdicts a person can force. ``'unknown'`` is absent on purpose -- overriding
a computed verdict back to "we do not know" is not a judgment, it is a
delete, and a delete of an override is spelled NULL."""


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
