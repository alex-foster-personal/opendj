"""``StateWriter``'s shared constants and free helpers -- the neutral base.

Split out of :mod:`apps.shared.state.writer` (quality-gate file_size
ratchet, round 4): both write-path mixins
(:mod:`apps.shared.state.writer_tracks`,
:mod:`apps.shared.state.writer_playlists`) need the table-name constants
and ``immediate_transaction``/``next_playlist_revision`` below. Living here,
with no import of ``writer`` or either mixin module, is what keeps the
three-way split acyclic: a module reachable via ``python -m`` (or any
direct import of a "leaf" submodule before its package's main module has
run) resolves a real cycle as one of the two sides partially initialized --
reproduced concretely in this round when ``writer.py`` first imported the
mixins from its own bottom half instead of routing through a common module
like this one. Neither ``writer`` nor either mixin imports the other two;
all three import only this module.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from . import sync_stamp as _sync_stamp

# The synced tables this class writes, and the primary key columns whose
# values become the ``local_changelog`` row_pk. Kept beside the writes rather
# than imported from apps.sync_hub: apps.shared must not depend on the sync
# app, and tests/shared/state/test_sync_stamp.py pins the two lists together.
TRACKS_TABLE: str = "tracks"
VENDOR_IDS_TABLE: str = "track_vendor_ids"
PLAYLISTS_TABLE: str = "playlists"
MEMBERSHIPS_TABLE: str = "playlist_memberships"


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    """Canonical sync timestamp for ``dt``.

    A naive datetime is read as UTC (long-standing behaviour of the injected
    test clocks) and then rendered in the one format the LWW comparison can
    order -- fixed-width microseconds, explicit ``+00:00`` (ADR 08 point 2).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return _sync_stamp.canonical_from(dt)


def compute_playlist_id(vendor: str, vendor_pl_id: str) -> str:
    """Stable playlist id: ``sha1('<vendor>:<vendor_pl_id>')``.

    Lets us round-trip a playlist between vendors without collisions while
    keeping the id deterministic.
    """
    return hashlib.sha1(f"{vendor}:{vendor_pl_id}".encode()).hexdigest()


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Acquire SQLite's writer lock before an authoritative read-modify-write.

    Membership replacement is destructive, so an ETag, member validation, and
    replacement must all happen after this lock is held.  Nested callers use a
    SAVEPOINT through :meth:`StateWriter._tx`; this outer primitive fails fast
    if a caller attempts to upgrade an already-open deferred transaction.
    """
    if conn.in_transaction:
        raise RuntimeError(
            "immediate transaction requires an idle connection; acquire it "
            "before reading membership state"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def next_playlist_revision(
    conn: sqlite3.Connection, playlist_id: str, candidate: str,
) -> str:
    """Return a revision distinct from the playlist's current ``updated_at``.

    Wall-clock timestamps normally differ at microsecond precision.  A fixed
    clock used by a deterministic caller must still rotate an ETag, so advance
    an equal ISO timestamp by one microsecond instead of silently reusing it.

    A STORED value this repo cannot order (a legacy naive stamp, SQLite's own
    ``CURRENT_TIMESTAMP`` spelling, a NULL) returns ``candidate`` unchanged.
    The bare ``datetime.fromisoformat`` this replaced raised ``TypeError``
    -- "can't compare offset-naive and offset-aware datetimes" -- from every
    playlist write touching such a row, which is an undeclared failure in
    five call sites that all wanted one thing: an ETag distinct from the
    stored one. ``candidate`` is offset-bearing and the stored value is not,
    so they cannot collide, which is exactly the property this function
    exists to guarantee.
    """
    row = conn.execute(
        "SELECT updated_at FROM playlists WHERE playlist_id = ?", (playlist_id,),
    ).fetchone()
    if row is None or not _sync_stamp.is_orderable(row[0]):
        return candidate
    previous = _sync_stamp.parse_canonical(str(row[0]))
    requested = _sync_stamp.parse_canonical(candidate)
    if requested > previous:
        return candidate
    return _iso(previous + timedelta(microseconds=1))


__all__ = [
    "MEMBERSHIPS_TABLE",
    "PLAYLISTS_TABLE",
    "TRACKS_TABLE",
    "VENDOR_IDS_TABLE",
    "compute_playlist_id",
    "immediate_transaction",
    "next_playlist_revision",
]
