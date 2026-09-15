"""Canonical sync stamps and the machine-local changelog.

Contract: ``specs/design_decision_08.md`` points 2 and 3, which answer round 1
findings A1 (no writer stamped ``updated_at``, so ``track_fields`` sync was
dead on arrival), 2 (ISO8601 string comparison is not an ordering over
instants) and 3 (the push watermark was a wall clock, so a skewed peer could
silently drop a local edit).

Three rules this module exists to enforce:

1. **One timestamp format.** :data:`CANONICAL_FORMAT` is UTC with fixed
   six-digit microseconds and an explicit ``+00:00`` offset. Under plain
   string comparison -- the only ordering the sync protocol uses -- two
   canonical stamps sort as their instants do. ``Z`` suffixes, second
   precision and non-UTC offsets all break that, so :func:`parse_canonical`
   rejects anything it cannot place on the UTC line and
   :func:`to_canonical` re-emits it in the one format.
2. **Every synced-table write is stamped and logged.** :func:`stamp_and_log`
   returns the ``(updated_at, origin_device_id)`` a writer must store AND
   appends one ``local_changelog`` row, in the caller's transaction. A row
   that reaches a synced table without passing through here syncs as epoch
   and loses every conflict, so this is a choke point by design.
3. **Identity comes from the data dir, never from a guess.** The
   ``machine-id`` file lives beside the DB rather than inside it (ADR 05
   section 1), so a DB restored onto another machine cannot inherit its
   identity. An in-memory DB has no data dir and raises rather than
   inventing one.

Round 3 added a fourth rule; round 5 replaced its second half, which had
been false for every reader it named since the day it was written:

4. **An unorderable value ALREADY IN THE DATABASE removes ONE ROW from the
   sync set. It does not stop the machine, and it is never guessed at.** The
   rule is a LOCAL-vs-WIRE split, and the two halves are deliberately
   asymmetric (round 2 finding N3b, round 3/4 finding R4, round 5):

   * **Off the wire**, :func:`parse_canonical` REFUSES an unorderable value.
     A peer sending a naive or unparseable stamp is a protocol violation to
     report, not a legacy row to tolerate -- storing it would put a row
     nothing can order into the sync set.
   * **Read back out of a LOCAL table**, an unorderable stored stamp (a
     legacy v5 write, a hand repair, SQLite's own ``CURRENT_TIMESTAMP``
     spelling ``'2024-11-01 12:00:00'``) QUARANTINES its row. The reader asks
     :func:`apps.sync_hub.protocol.stored_stamp_faults` first, then excludes
     that one row from the offer, the digest and the merge, counts it, and
     logs the offending value. Every other row syncs.

   Round 3 coalesced such a value to :data:`EPOCH` instead, and round 5
   removed that function outright. Coalescing bought availability by
   FABRICATING a value: a recent edit carrying an unorderable timestamp
   would be offered as year zero, lose to the peer's copy, and the peer's
   copy would then be pulled back over it -- an invisible, irreversible loss
   of a real edit, which is exactly what
   :func:`apps.sync_hub.engine_apply._duplicate_stamps` already refuses to
   do for a row with no sort key at all. A naive stamp is not missing
   information; it is recent information in a spelling we decline to trust.

   :data:`EPOCH` is a COMPARISON sentinel and nothing more: it is year zero,
   which ``datetime`` cannot represent, so :func:`parse_canonical` rejects it
   too. Nothing in this module returns it for STORAGE any more, so it cannot
   leak onto a row. :data:`FLOOR_STAMP` is the storable floor (year one,
   orderable and parseable) and has exactly two writers: the repair pass
   ``python -m apps.shared.state.normalize_stamps``, and
   :func:`backfill_local_machine_id` for a row whose stamp is NULL, where
   there is no verbatim value to log and the column is NOT NULL.

   "ONE ROW" names the row the STAMP is on; it is not the whole cost.
   Exclusion is TRANSITIVE over the foreign keys
   (:mod:`apps.sync_hub.sync_set`): a held track takes its locations, vendor
   ids and fields with it, and a playlist whose membership names it is held
   whole, because offering a child without its parent is a ``FOREIGN KEY``
   409 no retry gets past. Said here because the number an operator reads is
   the transitive one: an unorderable stamp can exclude additional rows
   that depend on it from the sync set.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import machine_identity

# UTC, fixed-width microseconds, explicit offset. See rule 1 above.
CANONICAL_FORMAT: str = "%Y-%m-%dT%H:%M:%S.%f+00:00"

# What a NULL stamp sorts as. Byte-identical to
# ``apps.sync_hub.protocol.EPOCH`` (ADR 04 c7) and duplicated for the same
# reason :func:`encode_row_pk` is: ``apps.shared`` must not depend on
# ``apps.sync_hub``. The two are pinned together by
# ``tests/shared/state/test_sync_stamp.py``.
#
# COMPARISON ONLY. Year zero is unrepresentable to ``datetime``, so a row
# that stored it would be refused on every later read -- and after round 5 no
# function in this module returns it for storage, so nothing can put it
# there. That is an invariant rather than a rule to remember: the rule "never
# write EPOCH back" had already been broken twice when it was checked.
EPOCH: str = "0000-01-01T00:00:00+00:00"

# The lowest stamp that can be STORED, and the only floor a writer may emit.
# Deliberately NOT :data:`EPOCH`: year one is representable, sorts above the
# comparison sentinel and below every real stamp, and round-trips through
# :func:`parse_canonical` on every platform.
#
# Literal, NOT canonical_from(datetime.min): strftime("%Y") does not zero-pad
# years < 1000 on glibc (Linux/CI, Python 3.11), yielding "1-01-01..." which
# datetime.fromisoformat then rejects, so a repair would write an unparseable
# replacement and never converge.
FLOOR_STAMP: str = "0001-01-01T00:00:00.000000+00:00"

LOCAL_CHANGELOG_TABLE: str = "local_changelog"

# The directory name the canonical layout puts between the data dir and the
# DB file: ``<data-dir>/state/state.db`` (spec D1, apps/sync_hub/client.py).
STATE_DIR_NAME: str = "state"


class SyncStampError(RuntimeError):
    """A write could not be stamped. Never swallowed, never defaulted."""


@dataclass(frozen=True)
class Stamp:
    """What a writer must store on the row it is about to write."""

    updated_at: str
    origin_device_id: str


# ----- canonical timestamps ------------------------------------------------


def canonical_from(value: datetime) -> str:
    """Render an aware datetime in :data:`CANONICAL_FORMAT`.

    A naive datetime raises: "assume UTC" is exactly the hidden default that
    made round 1 finding 2 possible.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise SyncStampError(
            f"cannot stamp a naive datetime {value!r}; sync timestamps must "
            f"carry a UTC offset."
        )
    # isoformat(timespec="microseconds"), NOT strftime(CANONICAL_FORMAT):
    # strftime("%Y") does not zero-pad years < 1000 on glibc (Linux/CI), so
    # the year-one FLOOR_STAMP sentinel would render "1-01-01..." there and
    # fail to round-trip. isoformat zero-pads the year on every platform and
    # is byte-identical to CANONICAL_FORMAT for every real (4-digit) year.
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def canonical_now() -> str:
    """This instant in :data:`CANONICAL_FORMAT`."""
    return canonical_from(datetime.now(UTC))


def parse_canonical(ts: str) -> datetime:
    """Parse any offset-bearing ISO8601 timestamp into an aware UTC datetime.

    Accepts the wire's dialects (``Z`` suffix, second precision, a non-UTC
    offset) so the protocol boundary can normalize what a peer sends.
    Rejects a naive value, a non-string and anything unparseable -- those
    would sort below every offset-bearing timestamp forever.
    """
    if not isinstance(ts, str) or not ts:
        raise SyncStampError(
            f"timestamp must be a non-empty string, got {ts!r}"
        )
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError as exc:
        raise SyncStampError(f"{ts!r} is not an ISO8601 timestamp: {exc}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SyncStampError(
            f"{ts!r} has no UTC offset; a naive timestamp cannot be ordered "
            f"against a stamped one."
        )
    return parsed.astimezone(UTC)


def to_canonical(ts: str) -> str:
    """``parse_canonical`` then :func:`canonical_from`. Idempotent."""
    return canonical_from(parse_canonical(ts))


def is_orderable(value: Any) -> bool:
    """True when this STORED value can be placed on the UTC line.

    The affirmative half of module rule 4, and the only question a local
    reader is allowed to ask about a stored stamp. There is deliberately no
    function that turns a NO into a substitute value: see the rule for what
    coalescing cost, and :mod:`apps.shared.state.normalize_stamps` for the
    one sanctioned way a quarantined row rejoins the sync set.

    Single predicate for the sync-set fence
    (:func:`apps.sync_hub.protocol_common.stored_stamp_faults`), digest
    exclusion, and :func:`apps.shared.state.normalize_stamps.scan` -- all
    three import this function and must not reimplement it.
    """
    if not isinstance(value, str):
        return False
    try:
        parse_canonical(value)
    except SyncStampError:
        return False
    return True


# ----- row keys ------------------------------------------------------------


def encode_row_pk(values: Sequence[Any]) -> str:
    """Changelog ``row_pk`` for a (possibly composite) primary key.

    Byte-identical to :func:`apps.sync_hub.protocol.encode_row_pk` -- a
    canonical JSON array, so a value containing the delimiter cannot forge
    another row's key. Duplicated rather than imported because
    ``apps.shared`` must not depend on ``apps.sync_hub``; the two are pinned
    together by ``tests/shared/state/test_sync_stamp.py``.
    """
    payload = [None if value is None else str(value) for value in values]
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


# ----- machine identity from a connection ----------------------------------


def _main_database_file(conn: sqlite3.Connection) -> Path:
    for _seq, name, filename in conn.execute("PRAGMA database_list"):
        if name == "main":
            if not filename:
                raise SyncStampError(
                    "this connection has no database file (in-memory or "
                    "temporary), so it has no data dir and no machine "
                    "identity; open the state DB with "
                    "apps.shared.state.db.open_rw instead."
                )
            return Path(str(filename)).resolve()
    raise SyncStampError("PRAGMA database_list returned no 'main' schema")


def data_dir_for_connection(conn: sqlite3.Connection) -> Path:
    """The data dir whose ``machine-id`` file owns this connection's writes.

    The canonical layout is ``<data-dir>/state/state.db``, so the data dir is
    two levels up. A DB that does not sit in a directory named ``state``
    (sidecars, tests, ad hoc copies) owns its own directory instead -- stated
    explicitly rather than reaching for a repo-wide default, so two DBs never
    quietly share one identity.
    """
    db_file = _main_database_file(conn)
    if db_file.parent.name == STATE_DIR_NAME:
        return db_file.parent.parent
    return db_file.parent


@dataclass(frozen=True)
class _CachedId:
    #: The id file's own bytes, or None when it could not be read. See below
    #: for why this is the bytes and not a stat field.
    contents: bytes | None
    machine_id: str


#: Per-data-dir id cache, keyed by the id FILE's CONTENTS rather than plain
#: memoization (round 3 finding N8c). A bare ``functools.cache`` returned the
#: id read on the process's FIRST call forever, even after the id file was
#: rewritten out from under a long-running daemon by a restore or an
#: operator re-mint.
#:
#: That fix keyed on ``st_mtime_ns``, which is one cheap ``stat()`` but is not
#: a reading of whether the file changed. Inode timestamps come from the
#: kernel's COARSE clock, which advances once per timer tick, so a rewrite
#: landing in the same tick as the read before it carries an IDENTICAL mtime
#: and the stale id is served exactly as it was before the fix. Not
#: theoretical: that is how
#: ``test_a_rewritten_machine_id_file_is_not_served_stale`` failed the first
#: time this suite ran on fast self-hosted CI hardware (Thu 3 Sep 2026).
#:
#: The id file is 32 bytes, so reading it costs about what statting it does
#: and DECIDES the question rather than inferring it from a clock. Same
#: reasoning as ``apps.analysis.backlog._content_token``, which digests bytes
#: for the same reason.
_id_cache: dict[str, _CachedId] = {}


def _id_file_bytes(path: Path) -> bytes | None:
    """The id file's bytes, or None when absent or unreadable.

    None never matches a cached entry, so an unreadable file always falls
    through to ``get_or_create_machine_id`` rather than serving a remembered
    id for a file nothing can currently read.
    """
    try:
        return path.read_bytes()
    except OSError:
        return None


def _cached_machine_id(data_dir: str) -> str:
    path = machine_identity.machine_id_path(Path(data_dir))
    contents = _id_file_bytes(path)
    cached = _id_cache.get(data_dir)
    if cached is not None and contents is not None and cached.contents == contents:
        return cached.machine_id
    machine_id = machine_identity.get_or_create_machine_id(Path(data_dir))
    _id_cache[data_dir] = _CachedId(contents=_id_file_bytes(path), machine_id=machine_id)
    return machine_id


def reset_machine_id_cache() -> None:
    """Drop the per-data-dir id cache. For tests that delete the id file."""
    _id_cache.clear()


def local_machine_id(conn: sqlite3.Connection) -> str:
    """This machine's id for ``conn``, minting the id file on first call.

    Read-safe: touches the data dir, never the database. Use
    :func:`ensure_local_machine` on a write path, where the ``machines`` row
    the foreign keys point at must also exist.
    """
    try:
        return _cached_machine_id(str(data_dir_for_connection(conn)))
    except machine_identity.MachineIdentityError as exc:
        raise SyncStampError(str(exc)) from exc


def ensure_local_machine(conn: sqlite3.Connection) -> str:
    """:func:`local_machine_id`, plus the ``machines`` row FKs require.

    ``track_locations.machine_id`` references ``machines(machine_id)``, so a
    write path must register before it inserts. Registration is idempotent
    and cheap after the first call (one indexed SELECT).
    """
    machine_id = local_machine_id(conn)
    row = conn.execute(
        "SELECT 1 FROM machines WHERE machine_id = ?", (machine_id,)
    ).fetchone()
    if row is not None:
        return machine_id
    try:
        machine_identity.register_machine(
            conn, data_dir=data_dir_for_connection(conn)
        )
    except machine_identity.MachineIdentityError as exc:
        raise SyncStampError(str(exc)) from exc
    return machine_id


# ----- atomicity -----------------------------------------------------------

_SAVEPOINT: str = "sync_stamp_unit"


@contextmanager
def stamped_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One all-or-nothing unit around a row write and its changelog entry.

    :func:`stamp_and_log` promises the changelog append happens "in the
    caller's transaction". On a connection opened with
    ``isolation_level=None`` (every handle :mod:`apps.shared.state.db` hands
    out) there IS no caller transaction unless somebody opens one, so the
    promise was only as good as each writer's discipline -- and round 2
    finding N3a caught the backfill mid-loop with the row UPDATE committed
    and its changelog INSERT not.

    Nested calls use a SAVEPOINT so a writer already inside a transaction
    keeps one unit rather than committing its caller's work early.
    """
    nested = conn.in_transaction
    conn.execute(f"SAVEPOINT {_SAVEPOINT}" if nested else "BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if nested:
            conn.execute(f"ROLLBACK TO {_SAVEPOINT}")
            conn.execute(f"RELEASE {_SAVEPOINT}")
        else:
            conn.execute("ROLLBACK")
        raise
    conn.execute(f"RELEASE {_SAVEPOINT}" if nested else "COMMIT")


# ----- the choke point -----------------------------------------------------


def stamp_and_log(
    conn: sqlite3.Connection,
    table: str,
    row_pk: Sequence[Any],
    machine_id: str,
    now: str | None = None,
) -> Stamp:
    """Stamp one synced-table write and append it to ``local_changelog``.

    Returns the ``(updated_at, origin_device_id)`` the caller must store on
    the row it is writing. The changelog append happens in the caller's
    transaction, so a rolled-back write leaves no changelog entry and the
    push fence cannot offer a row that does not exist.

    ``now`` must already be canonical when supplied; it is re-parsed rather
    than trusted, so a writer with its own clock cannot smuggle a naive or
    non-UTC stamp into the sync set.
    """
    if not table:
        raise SyncStampError("stamp_and_log needs a table name")
    if not machine_id:
        raise SyncStampError(
            f"stamp_and_log({table!r}) needs a machine id; the write would "
            f"otherwise carry no origin and lose every LWW tiebreak."
        )
    stamped_at = canonical_now() if now is None else to_canonical(now)
    conn.execute(
        f"INSERT INTO {LOCAL_CHANGELOG_TABLE}("
        "table_name, row_pk, updated_at, origin_device_id, received_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (table, encode_row_pk(row_pk), stamped_at, machine_id, stamped_at),
    )
    return Stamp(updated_at=stamped_at, origin_device_id=machine_id)


# ----- post-migration backfill ---------------------------------------------


def backfill_local_machine_id(conn: sqlite3.Connection) -> int:
    """Claim this machine's unstamped ``track_locations`` rows. Returns count.

    Migration v6 cannot know the machine id (schema.py reading 3), so rows
    carried over from v5 arrive with ``machine_id`` NULL. They describe files
    on THIS machine -- v4 seeded them from ``tracks.file_path`` locally -- so
    this machine claims them, stamps the origin it should always have had,
    and logs them for the next push.

    A no-op when there is nothing to claim: a fresh DB never mints an id file
    or a ``machines`` row just by being opened.

    All-or-nothing (round 2 finding N3a). The whole claim runs in one
    :func:`stamped_transaction`, so a failure part-way leaves NOTHING
    claimed and the retry -- whose predicate is ``machine_id IS NULL`` --
    actually retries instead of reporting success over half-claimed rows.

    A legacy ``updated_at`` this module cannot order does not abort it
    either: the changelog entry carries the row's stamp VERBATIM (round 5).
    Nothing orders ``local_changelog.updated_at`` -- the push fence reads
    ``seq, table_name, row_pk`` -- so the honest value is the row's own, and
    ``normalize_stamps`` sweeps this column, so one repair pass fixes the row
    and its changelog entry together. Round 3 wrote :data:`EPOCH` here, which
    put a year-zero value nothing can parse into the database on an ordinary
    open. Only a NULL stamp gets a substitute, :data:`FLOOR_STAMP`, because
    the column is NOT NULL and there is no verbatim value to write.

    The gate read outside the transaction is deliberate: the common case is
    "nothing to claim" on every writable open, and that case must not take
    SQLite's write lock. The authoritative read happens again inside.
    """
    if not conn.execute(
        "SELECT 1 FROM track_locations WHERE machine_id IS NULL LIMIT 1"
    ).fetchone():
        return 0
    with stamped_transaction(conn):
        pending = conn.execute(
            "SELECT location_id, updated_at FROM track_locations "
            "WHERE machine_id IS NULL"
        ).fetchall()
        if not pending:
            return 0
        machine_id = ensure_local_machine(conn)
        received_at = canonical_now()
        for location_id, updated_at in pending:
            conn.execute(
                "UPDATE track_locations SET machine_id = ?, "
                "origin_device_id = COALESCE(origin_device_id, ?) "
                "WHERE location_id = ?",
                (machine_id, machine_id, location_id),
            )
            conn.execute(
                f"INSERT INTO {LOCAL_CHANGELOG_TABLE}("
                "table_name, row_pk, updated_at, origin_device_id, received_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "track_locations",
                    encode_row_pk((location_id,)),
                    FLOOR_STAMP if updated_at is None else str(updated_at),
                    machine_id,
                    received_at,
                ),
            )
    return len(pending)


__all__ = [
    "CANONICAL_FORMAT",
    "EPOCH",
    "FLOOR_STAMP",
    "LOCAL_CHANGELOG_TABLE",
    "Stamp",
    "SyncStampError",
    "backfill_local_machine_id",
    "canonical_from",
    "canonical_now",
    "data_dir_for_connection",
    "encode_row_pk",
    "ensure_local_machine",
    "is_orderable",
    "local_machine_id",
    "parse_canonical",
    "reset_machine_id_cache",
    "stamp_and_log",
    "stamped_transaction",
    "to_canonical",
]
