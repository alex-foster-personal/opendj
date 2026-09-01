"""One-shot repair for stored timestamps the sync protocol cannot order.

Contract: round 2 finding N3b, triage item 2. ``apps.shared.state.sync_stamp``
rule 4 makes an unorderable STORED stamp survivable -- it reads as
:data:`apps.shared.state.sync_stamp.EPOCH` instead of aborting every push
forever. Surviving is not the same as being right: a row stuck at EPOCH loses
every conflict it takes part in, silently. This module is the repair pass that
puts such rows back on the UTC line, and it is deliberately a separate,
explicit, operator-run step rather than something an open does behind your
back.

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

#: The lowest stamp that can be STORED. See the module docstring: this is
#: deliberately not ``sync_stamp.EPOCH``, which is year zero and therefore
#: unrepresentable and unparseable -- writing that would recreate the brick.
# Literal, NOT sync_stamp.canonical_from(datetime.min): strftime("%Y") does not
# zero-pad years < 1000 on glibc (Linux/CI, Python 3.11), yielding "1-01-01..."
# which datetime.fromisoformat then rejects, so the repair would write an
# unparseable replacement and never converge. The literal is a valid ISO8601
# year-one stamp that parse_canonical accepts on every platform.
FLOOR_STAMP: str = "0001-01-01T00:00:00.000000+00:00"

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
)


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


def apply_repairs(conn: sqlite3.Connection, repairs: list[Repair]) -> int:
    """Write every repair in one transaction. Returns the number applied."""
    if not repairs:
        return 0
    with sync_stamp.stamped_transaction(conn):
        for repair in repairs:
            conn.execute(
                f"UPDATE {repair.table} SET {repair.column} = ? WHERE rowid = ?",
                (repair.replacement, repair.rowid),
            )
    return len(repairs)


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
        if not repairs:
            print("[OK] every stored timestamp is orderable; nothing to do")
            return 0
        if args.dry_run:
            print(f"[DRY-RUN] {len(repairs)} value(s) would be rewritten")
            return 0
        applied = apply_repairs(conn, repairs)
        print(f"[OK] rewrote {applied} value(s)")
        return 0
    finally:
        conn.close()


__all__ = [
    "FLOOR_STAMP",
    "STAMP_COLUMNS",
    "Repair",
    "apply_repairs",
    "main",
    "scan",
    "state_db_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
