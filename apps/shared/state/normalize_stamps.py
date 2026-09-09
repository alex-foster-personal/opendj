"""One-shot repair for stored timestamps the sync protocol cannot order.

Contract: round 2 finding N3b, triage item 2; round 5. ``apps.shared.state.
sync_stamp`` rule 4 makes an unorderable STORED stamp survivable -- the row
is QUARANTINED, excluded from the offer, the digest and the merge, counted
and logged, while every other row syncs. Surviving is not the same as being
right: a quarantined row reaches no peer at all.

**This pass is now the ONLY way a quarantined row rejoins the sync set.**
Nothing else repairs one, on purpose. It is a separate, explicit,
operator-run step rather than something an open does behind your back,
because ``naive-assumed-utc`` IS an assumption -- a legitimate one, stated,
printed per row and gated behind ``--live``, which is exactly what makes it
legitimate. Moving that assumption into a read path would make it happen
silently on every sync, which is how a guess becomes indistinguishable from
a fact.

Two repairs, and the difference between them matters:

* ``naive-assumed-utc`` -- the value parses as a date and time but carries no
  offset (``'2024-11-01T12:00:00'``, or SQLite's own ``CURRENT_TIMESTAMP``
  spelling ``'2024-11-01 12:00:00'``). It is re-emitted as that wall time in
  UTC. This IS an assumption, which is why it lives behind a CLI flag and is
  printed per row rather than applied by a library call: SQLite's
  ``CURRENT_TIMESTAMP`` is UTC, and every in-repo writer has always emitted
  UTC, so UTC is the only reading with evidence behind it.
* ``unparseable-to-floor`` -- the value is not a timestamp at all. Nothing can
  recover an instant from it, so it is written as :data:`FLOOR_STAMP`, the
  lowest timestamp that can actually be stored, making the row's loss of
  every conflict visible in the data instead of implied by a rejector.

  Note that :data:`FLOOR_STAMP` is NOT ``sync_stamp.EPOCH``.
  ``EPOCH`` is year zero, which ``datetime`` cannot represent, so it is a
  comparison sentinel and nothing else: a row that literally STORED it would
  be refused by ``protocol.canonical_timestamp`` on every read -- the exact
  brick this pass exists to clear. ``FLOOR_STAMP`` is year one, which sorts
  above the sentinel and below every real stamp, and parses.

What this does NOT touch: a value that parses and carries an offset, even in
a non-canonical spelling (``...Z``, second precision, ``+01:00``). Those are
orderable, the protocol boundary re-emits them canonically on every read, and
rewriting them would churn rows for no correctness gain.

Usage (nothing happens without ``--live``)::

    python -m apps.shared.state.normalize_stamps --data-dir data --dry-run
    python -m apps.shared.state.normalize_stamps --data-dir data --live
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from . import db as state_db
from . import sync_stamp

Reason = Literal["naive-assumed-utc", "unparseable-to-floor"]

#: The lowest stamp that can be STORED, re-exported from the module that owns
#: stamp semantics so the two literals cannot drift. See the module docstring:
#: deliberately not ``sync_stamp.EPOCH``, which is year zero and therefore
#: unrepresentable and unparseable -- writing that would recreate the brick.
FLOOR_STAMP: str = sync_stamp.FLOOR_STAMP

#: Every table/column pair whose value the sync protocol orders or hashes.
#: Mirrors ``apps.sync_hub.protocol.DIGEST_TABLES`` x its timestamp columns,
#: plus the spoke-side changelog. Duplicated rather than imported for the
#: reason ``sync_stamp.encode_row_pk`` gives (``apps.shared`` must not depend
#: on ``apps.sync_hub``); pinned by
#: ``tests/shared/state/test_normalize_stamps.py``.
STAMP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("playlist_memberships", "updated_at"),
    ("playlist_memberships", "deleted_at"),
    ("playlist_pins", "updated_at"),
    ("playlist_pins", "deleted_at"),
    ("playlists", "updated_at"),
    ("playlists", "deleted_at"),
    ("sync_policies", "updated_at"),
    ("sync_policies", "deleted_at"),
    ("track_fields", "updated_at"),
    ("track_fields", "deleted_at"),
    ("track_locations", "updated_at"),
    ("track_locations", "deleted_at"),
    ("track_vendor_ids", "updated_at"),
    ("track_vendor_ids", "deleted_at"),
    ("tracks", "updated_at"),
    ("tracks", "deleted_at"),
    (sync_stamp.LOCAL_CHANGELOG_TABLE, "updated_at"),
    (sync_stamp.LOCAL_CHANGELOG_TABLE, "received_at"),
    # The HUB-side changelog too (round 5). It carries the stamps a spoke
    # pushed, so a hub that merged a row before this pass existed can hold an
    # unorderable one; ``scan`` skips a table that does not exist, so naming
    # it here is safe on a spoke that has no hub_changelog.
    ("hub_changelog", "updated_at"),
    ("hub_changelog", "received_at"),
)


#: Primary key columns of every SYNCED table, so a repaired row can be named
#: in ``local_changelog``. Mirrors ``apps.sync_hub.protocol.SYNC_TABLES`` (plus
#: the membership table); duplicated rather than imported for the reason
#: ``sync_stamp.encode_row_pk`` gives, and pinned by
#: ``tests/shared/state/test_normalize_stamps.py``. A table NOT in here (the
#: two changelogs) is repaired without a changelog entry, which is right: they
#: are machine-local bookkeeping and never sync.
SYNCED_PKS: dict[str, tuple[str, ...]] = {
    "playlist_memberships": ("playlist_id", "position"),
    "playlist_pins": ("machine_id", "playlist_id"),
    "playlists": ("playlist_id",),
    "sync_policies": ("machine_id", "asset_kind"),
    "track_fields": ("stable_id", "field_name"),
    "track_locations": ("location_id",),
    "track_vendor_ids": ("stable_id", "vendor"),
    "tracks": ("stable_id",),
}


@dataclass(frozen=True)
class Repair:
    """One stored value this pass would rewrite, and why."""

    table: str
    column: str
    rowid: int
    stored: str
    replacement: str
    reason: Reason

    def describe(self) -> str:
        return (
            f"{self.table}.{self.column} rowid={self.rowid} "
            f"{self.stored!r} -> {self.replacement!r} [{self.reason}]"
        )


def _repair_for(value: str) -> tuple[str, Reason]:
    """The canonical replacement for an unorderable stored value."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return FLOOR_STAMP, "unparseable-to-floor"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return (
            sync_stamp.canonical_from(parsed.replace(tzinfo=UTC)),
            "naive-assumed-utc",
        )
    # Offset-bearing values are orderable by definition, so scan() never
    # reaches here; keeping the branch explicit beats an unreachable else.
    return sync_stamp.to_canonical(value), "naive-assumed-utc"


def _is_orderable(value: str) -> bool:
    try:
        sync_stamp.parse_canonical(value)
    except sync_stamp.SyncStampError:
        return False
    return True


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def scan(conn: sqlite3.Connection) -> list[Repair]:
    """Every stored timestamp the protocol could not order, with its fix.

    A table missing from the DB is skipped rather than raising: this pass is
    run against legacy files, and refusing to repair the fifteen tables that
    DO exist because a sixteenth predates migration v6 would make it useless
    exactly where it is needed.
    """
    repairs: list[Repair] = []
    for table, column in STAMP_COLUMNS:
        if not _table_exists(conn, table):
            continue
        rows = conn.execute(
            f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL"
        ).fetchall()
        for rowid, stored in rows:
            value = str(stored)
            if _is_orderable(value):
                continue
            replacement, reason = _repair_for(value)
            repairs.append(
                Repair(
                    table=table,
                    column=column,
                    rowid=int(rowid),
                    stored=value,
                    replacement=replacement,
                    reason=reason,
                )
            )
    return repairs


def _log_repaired_row(conn: sqlite3.Connection, table: str, rowid: int) -> None:
    """Append the ``local_changelog`` entry a repaired row needs to be offered.

    Without this the pass repairs a row and the push fence never sees it: a
    machine that has ALREADY completed one sync offers only what
    ``local_changelog`` recorded above its watermark, so a row freed from
    quarantine stayed out of the sync set forever and the post-sync digest
    compare failed permanently. Reproduced live while round 5 was being
    built, against the real library: repair, sync, ``SyncDigestMismatch`` on
    ``['playlist_memberships', 'playlists', 'track_locations',
    'track_vendor_ids', 'tracks']``.

    The entry carries the row's OWN (now orderable) stamp and origin, not a
    fresh one: the repair re-spells a timestamp, it does not author an edit,
    and bumping the stamp would make the repaired row win conflicts it should
    lose.
    """
    pk_columns = SYNCED_PKS.get(table)
    if pk_columns is None:
        return
    row = conn.execute(
        f"SELECT {', '.join(pk_columns)}, updated_at, origin_device_id "
        f"FROM {table} WHERE rowid = ?",
        (rowid,),
    ).fetchone()
    if row is None:
        raise SystemExit(
            f"{table} rowid={rowid} vanished between the scan and the repair"
        )
    pk = tuple(row[: len(pk_columns)])
    stamp = row[len(pk_columns)]
    origin = row[len(pk_columns) + 1]
    encoded_pk = sync_stamp.encode_row_pk(pk)
    stamp_value = sync_stamp.FLOOR_STAMP if stamp is None else str(stamp)
    origin_value = "" if origin is None else str(origin)
    received_at = sync_stamp.canonical_now()
    conn.execute(
        f"INSERT INTO {sync_stamp.LOCAL_CHANGELOG_TABLE}("
        "table_name, row_pk, updated_at, origin_device_id, received_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (table, encoded_pk, stamp_value, origin_value, received_at),
    )
    # ``local_changelog`` only reaches a PEER through this machine's own next
    # push. When this pass runs directly on a HUB db, the repaired row
    # already lives in ``hub_changelog`` at its ORIGINAL seq, with the
    # unorderable stamp it was quarantined for -- pull reads each named row
    # LIVE (apps.sync_hub.engine_changes._changelog_rows), but only for
    # entries above a spoke's ``since_seq``, and a spoke that already pulled
    # that seq will never ask for it again. Appending a fresh hub_changelog
    # row gives the repair a new, higher seq so the next pull picks it up.
    # Harmless on a spoke db: nothing ever calls ``hub_changes_since``
    # against a spoke's own (unused) hub_changelog table.
    #
    # This requeues THIS ROW ONLY. Quarantine is transitive, so repairing a
    # parent also frees dependants this pass never touched, and they get no
    # entry here -- requeueing the closure would put a second copy of the
    # foreign-key graph in this module (see :func:`reset_sync_fences` for
    # why that is refused). On a hub the operator closes that gap with a
    # generation rotate; :data:`HUB_ROTATE_NOTICE` is where they are told so.
    if _table_exists(conn, "hub_changelog"):
        conn.execute(
            "INSERT INTO hub_changelog("
            "table_name, row_pk, updated_at, origin_device_id, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (table, encoded_pk, stamp_value, origin_value, received_at),
        )


def reset_sync_fences(conn: sqlite3.Connection) -> int:
    """Reset BOTH floors against every peer. Returns the peers reset.

    A repair frees rows that were held back, and quarantine is TRANSITIVE
    (:mod:`apps.sync_hub.sync_set`), so it frees rows it never touched: a
    repaired track's ``track_fields``, the playlists whose membership bundles
    named it, and those playlists' pins. Re-logging exactly that closure
    would put a second copy of the foreign-key graph in this module, and two
    copies drift.

    The watermark is the primitive this codebase already uses for "re-offer
    everything and let LWW reject what has not changed":
    ``apps.sync_hub.client._watermark_after_hello`` does precisely this after
    a hub restore -- and it resets BOTH floors, which is the half this pass
    got wrong until the round 5 gate. ``last_sync_at`` is what
    ``Watermark.needs_full_offer`` reads, so clearing it -- not just the seq
    -- is what reaches rows that never had a changelog entry at all, which is
    every row of a library that predates the changelog. Round 1 proved the
    replay idempotent three deep.

    ``last_pull_seq`` matters for the OTHER direction, and it is not
    symmetry for its own sake. While a local row cannot be ordered,
    ``apps.sync_hub.engine_apply`` REFUSES the peer's copy of it, and the
    pull watermark advances over that entry regardless -- a refusal is not a
    replay queue. So a repair that reset only the push fence left the peer's
    edit permanently unreachable: the repaired local row lost LWW to the
    hub's newer copy, the hub had no changelog entry above the pull fence to
    re-send, and every later sync raised ``SyncDigestMismatch`` forever while
    the peer reported success. The repair is the only exit quarantine offers,
    so it has to clear both sides of it.

    Observed with the push fence alone: repair, sync, ``SyncDigestMismatch``
    on ``['playlist_memberships', 'playlists', 'track_vendor_ids']`` -- the
    repaired track went, its cascade-held dependants did not; and, on the
    pull side, a peer's newer title that never arrived across four more
    syncs.

    PUBLIC, and unconditional on this machine having anything to repair
    (round 5 gate B1c). The quarantine log tells an operator to run this pass
    on the machine that HOLDS the unorderable row. When that machine is the
    HUB, the SPOKE has nothing to repair -- and it is the spoke whose pull
    fence has already advanced past the changelog entries the hub's freed
    rows were skipped in, so the spoke is exactly where the reset has to
    happen. A version of this that only ran when ``repairs`` was non-empty
    made the printed recovery instruction FALSE on the one machine that
    needed it. Runs in the caller's transaction when it has one, and opens
    its own when called alone.
    """
    if not _table_exists(conn, "sync_state"):
        return 0
    cursor = conn.execute(
        "UPDATE sync_state SET last_push_seq = 0, last_pull_seq = 0, "
        "last_sync_at = NULL"
    )
    return int(cursor.rowcount or 0)


@dataclass(frozen=True)
class RepairOutcome:
    """What one live pass did. Two numbers, because they are two facts.

    ``applied`` can be 0 on the machine that most needs ``fences_reset`` to
    be non-zero -- the spoke whose unorderable row lives on the HUB. Folding
    them into one count is what made the printed recovery instruction false
    (round 5 gate B1c).
    """

    applied: int
    fences_reset: int


def apply_repairs(conn: sqlite3.Connection, repairs: list[Repair]) -> RepairOutcome:
    """Write every repair in one transaction. Returns what it did.

    Two side effects, both load-bearing and both discovered by running this
    against the real library rather than reasoned about in advance:

    * each repaired row in a SYNCED table gets one ``local_changelog`` entry
      (:func:`_log_repaired_row`) -- one per ROW, not per value, since a row
      whose ``updated_at`` and ``deleted_at`` were both repaired changed once;
    * every peer's push AND pull fence is reset
      (:func:`reset_sync_fences`), which is what reaches the rows this pass
      FREED without touching, and what re-delivers the rows the peer already
      sent while this machine could not compare them.

    An EMPTY ``repairs`` list still resets the fences (round 5 gate B1c). It
    is not a no-op case: the operator was told to run this because a peer
    quarantined a row, and when the unorderable row is on the OTHER machine
    this one has nothing to rewrite and everything to re-pull. The replay
    that follows is idempotent -- LWW rejects on equality, proven three deep
    in round 1 -- so the cost of the reset is a re-offer, and the cost of
    skipping it was permanent loss.
    """
    with sync_stamp.stamped_transaction(conn):
        applied: list[Repair] = []
        for repair in repairs:
            # ``scan()`` ran before this transaction's write lock
            # (``stamped_transaction`` -> ``BEGIN IMMEDIATE``), so a
            # concurrent writer could have stored a newer value in the gap.
            # Condition the write on the column still holding what ``scan``
            # read: an unconditional ``WHERE rowid = ?`` would clobber that
            # newer value and silently change LWW ordering for the row.
            cursor = conn.execute(
                f"UPDATE {repair.table} SET {repair.column} = ? "
                f"WHERE rowid = ? AND {repair.column} = ?",
                (repair.replacement, repair.rowid, repair.stored),
            )
            if cursor.rowcount:
                applied.append(repair)
        for table, rowid in sorted({(r.table, r.rowid) for r in applied}):
            _log_repaired_row(conn, table, rowid)
        fences = reset_sync_fences(conn)
    return RepairOutcome(applied=len(applied), fences_reset=fences)


#: Printed after every live repair, because "[OK] rewrote N value(s)" on its
#: own is only true for a SPOKE.
#:
#: A repair frees rows TRANSITIVELY (``apps.sync_hub.sync_set``), and the two
#: things that re-deliver the freed closure are both spoke-side:
#: :func:`reset_sync_fences` rewrites ``sync_state``, whose only writer in
#: the repo is ``apps.sync_hub.client``'s spoke loop, so on a hub db it
#: matches zero rows; and :func:`_log_repaired_row` appends ONE fresh
#: ``hub_changelog`` entry for the row it repaired, not for the dependants
#: that row was holding back. Every spoke has already advanced
#: ``last_pull_seq`` past the entries those dependants were skipped in, and
#: pull never revisits a seq, so they stay unreachable and the next digest
#: compare mismatches permanently.
#:
#: The escape is a generation rotate, which is what makes every spoke reset
#: its watermarks and re-offer (``apps.sync_hub.client._watermark_after_hello``).
#: This module cannot RUN it -- ``apps.shared`` must not depend on
#: ``apps.sync_hub`` (see ``sync_stamp.encode_row_pk``), and a repair pass
#: silently forcing a fleet-wide full re-offer would be a far larger side
#: effect than the one the operator asked for. So it is NAMED here instead of
#: performed, and named unconditionally: this pass has no honest way to tell a
#: hub db from a spoke db (both carry a ``hub_changelog`` table), and a
#: guessed discriminator printing the wrong half of this is worse than
#: printing both.
HUB_ROTATE_NOTICE: str = (
    "[NOTE] If this database is the HUB, the repair is not finished. Run\n"
    "       `python -m apps.sync_hub rotate --data-dir DIR` on the hub now.\n"
    "       Repairing a row also frees the rows it was holding back, and\n"
    "       every spoke has already pulled past the changelog entries those\n"
    "       rows were skipped in -- a plain re-sync will NOT re-deliver them,\n"
    "       and the post-sync digest compare will mismatch until it does.\n"
    "       On a spoke there is nothing further to do: syncing is enough."
)


#: Said on every ``--live`` run, including one that rewrote nothing, because
#: the fence reset is the half of this pass that a machine with no local fault
#: still needs. The quarantine log names the machine that HOLDS the bad row;
#: the machine that has to RE-PULL what the repair frees is its peer, and on
#: that peer ``scan`` finds nothing.
FENCE_RESET_NOTICE: str = (
    "the next sync re-offers this machine's library and re-pulls the peer's, "
    "so rows freed by a repair on EITHER machine are exchanged again"
)


def state_db_path(data_dir: Path) -> Path:
    """``<data-dir>/state/state.db`` -- the layout spec D1 fixes."""
    return Path(data_dir) / sync_stamp.STATE_DIR_NAME / "state.db"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m apps.shared.state.normalize_stamps",
        description=(
            "Repair stored timestamps the sync protocol cannot order "
            "(round 2 finding N3b)."
        ),
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        type=Path,
        help="data dir holding state/state.db",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true", help="report repairs, write nothing"
    )
    mode.add_argument("--live", action="store_true", help="apply the repairs")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    target = state_db_path(args.data_dir)
    if not target.exists():
        print(f"[ERROR] no state DB at {target}", file=sys.stderr)
        return 2
    conn = state_db.open_rw(target)
    try:
        repairs = scan(conn)
        for repair in repairs:
            print(repair.describe())
        if args.dry_run:
            if not repairs:
                print("[OK] every stored timestamp is orderable; nothing to rewrite")
            else:
                print(f"[DRY-RUN] {len(repairs)} value(s) would be rewritten")
            print(f"[DRY-RUN] {FENCE_RESET_NOTICE}")
            return 0
        # --live ALWAYS runs, including with zero repairs (round 5 gate
        # B1c). An operator reaching for this command was told to by a
        # quarantine log, and on the machine whose peer holds the
        # unorderable row there is nothing to rewrite and a fence to reset.
        outcome = apply_repairs(conn, repairs)
        if outcome.applied:
            print(f"[OK] rewrote {outcome.applied} value(s)")
        else:
            print("[OK] every stored timestamp on this machine is orderable")
        print(
            f"[OK] reset the push and pull fence against "
            f"{outcome.fences_reset} peer(s); {FENCE_RESET_NOTICE}"
        )
        print(HUB_ROTATE_NOTICE)
        return 0
    finally:
        conn.close()


__all__ = [
    "FENCE_RESET_NOTICE",
    "FLOOR_STAMP",
    "HUB_ROTATE_NOTICE",
    "STAMP_COLUMNS",
    "SYNCED_PKS",
    "Repair",
    "RepairOutcome",
    "apply_repairs",
    "main",
    "reset_sync_fences",
    "scan",
    "state_db_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
