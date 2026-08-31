"""One-shot NFC repair for ``track_locations`` paths already in the database.

Contract: round 3 finding R3
(``.planning/cloudsync-round3-adversarial.md``), triage item 3.
:func:`apps.shared.state.locations.normalize_stored_text` NFC-normalizes
every path and URL written from round 3 onward, so two Unicode spellings of
one file resolve to a single row before the partial UNIQUE index ever sees
them (round 2 finding N2). It does nothing for what is ALREADY stored: a row
written on this Mac before that commit came from a macOS filesystem walk,
which hands back NFD, and a stored NFD row does not match the NFC lookup, so
the next upsert mints a SECOND row. Both spellings canonicalize to the same
NFC bytes on the wire (``apps.sync_hub.protocol.nfc``), the hub collapses them
to one and prunes the loser's changelog, the spoke keeps two, and every later
sync fails its ``track_locations`` digest compare permanently -- N2's brick,
reproduced with no repair path. This module is that repair path, one table
over from :mod:`apps.shared.state.normalize_stamps`.

What it does, per natural key
-----------------------------
The logical identity of a row is
``(stable_id, machine_id, kind, file_path)`` for a local row and
``(stable_id, machine_id, kind, remote_url)`` for a remote one -- exactly the
two partial UNIQUE indexes (schema v6). This pass groups rows by that key
AFTER NFC-normalizing the path, so an NFD row and its NFC twin land in one
group, and:

* **a lone NFD row** (no twin) has its stored path rewritten to NFC in place;
* **an NFD row plus its NFC twin** collapse to ONE row -- the last-writer-wins
  winner survives (``(updated_at, origin_device_id)``, the same comparison
  ``apps.sync_hub.protocol.lww_key`` makes), the loser is hard-deleted, and
  the survivor's path is stored NFC.

The loser is hard-deleted, not tombstoned, for the reason
``apps.sync_hub.engine._drop_superseded`` gives: a partial UNIQUE index cannot
hold a tombstone and its NFC replacement at once, and a tombstone still
carries the path so it would still collide. The survivor's ``local_changelog``
entry (carrying its EXISTING ``updated_at``, never a fresh one -- the winner
won BECAUSE of that stamp) is what propagates the collapse: a peer receives
the winner, resolves it against its own copy on the same natural key, and
drops its own duplicate by this same path. The loser's dangling changelog
entries are pruned so the next push cannot try to offer a row that is gone
(round 1 finding 4a's blast radius, in miniature).

What it does NOT touch: a row whose path is already NFC and has no twin.
Domain text (titles, etc.) is out of scope -- the digest NFC-normalizes it on
the wire so it never bricks, and rewriting it in storage would churn rows for
no correctness gain (finding R3's own note).

Usage (nothing happens without ``--live``)::

    python -m apps.shared.state.normalize_locations --data-dir data --dry-run
    python -m apps.shared.state.normalize_locations --data-dir data --live
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from . import db as state_db
from . import sync_stamp

#: The synced table this pass repairs, and the two changelog tables whose
#: dangling entries for a dropped loser must be pruned alongside it. Mirrors
#: ``apps.sync_hub.engine._drop_superseded``; duplicated rather than imported
#: because ``apps.shared`` must not depend on ``apps.sync_hub``.
LOCATIONS_TABLE: str = "track_locations"
CHANGELOG_TABLES: tuple[str, ...] = ("hub_changelog", sync_stamp.LOCAL_CHANGELOG_TABLE)


def _nfc(value: str | None) -> str | None:
    """NFC-normalize a path/URL, or ``None`` for a NULL column."""
    return None if value is None else unicodedata.normalize("NFC", value)


def _is_nfc(value: str | None) -> bool:
    """A NULL or an already-NFC string needs no rewrite."""
    return value is None or unicodedata.is_normalized("NFC", value)


@dataclass(frozen=True)
class LocationRow:
    """One ``track_locations`` row, only the columns this pass reasons about."""

    location_id: str
    machine_id: str | None
    stable_id: str
    kind: str
    file_path: str | None
    remote_url: str | None
    updated_at: str | None
    origin_device_id: str | None

    @property
    def stored_is_nfc(self) -> bool:
        return _is_nfc(self.file_path) and _is_nfc(self.remote_url)

    @property
    def lww_key(self) -> tuple[str, str]:
        """``(updated_at, origin_device_id)``, the winner-picks-max comparison.

        An unorderable or NULL stored stamp coalesces to
        :data:`apps.shared.state.sync_stamp.EPOCH` (module rule 4) rather than
        raising, so one legacy row cannot crash the repair that would fix it.
        A NULL origin collapses to ``''`` (``protocol.NO_ORIGIN``), keeping the
        order total.
        """
        return (
            sync_stamp.coalesce_stored_stamp(self.updated_at),
            self.origin_device_id or "",
        )

    @property
    def normalized_key(self) -> tuple[str | None, str, str, str | None, str | None]:
        """The natural key with its path/URL NFC-normalized.

        Two spellings of one file share this key; that is what puts an NFD row
        and its NFC twin in one group.
        """
        return (
            self.machine_id,
            self.stable_id,
            self.kind,
            _nfc(self.file_path),
            _nfc(self.remote_url),
        )


@dataclass(frozen=True)
class Collapse:
    """One natural key this pass repairs: the survivor and the rows it absorbs."""

    winner: LocationRow
    losers: tuple[LocationRow, ...]
    nfc_file_path: str | None
    nfc_remote_url: str | None

    @property
    def path_rewritten(self) -> bool:
        return (
            self.winner.file_path != self.nfc_file_path
            or self.winner.remote_url != self.nfc_remote_url
        )

    def describe(self) -> str:
        target = self.nfc_remote_url if self.nfc_file_path is None else self.nfc_file_path
        if self.losers:
            losing = ", ".join(loser.location_id for loser in self.losers)
            return (
                f"{LOCATIONS_TABLE} {self.winner.machine_id}/{self.winner.stable_id}"
                f"/{self.winner.kind}: winner {self.winner.location_id} keeps "
                f"{target!r}; drops {losing} (lost LWW)"
            )
        return (
            f"{LOCATIONS_TABLE} {self.winner.machine_id}/{self.winner.stable_id}"
            f"/{self.winner.kind}: {self.winner.location_id} path "
            f"{'->'} NFC {target!r} (no twin, rewritten in place)"
        )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _read_rows(conn: sqlite3.Connection) -> list[LocationRow]:
    return [
        LocationRow(
            location_id=str(row[0]),
            machine_id=None if row[1] is None else str(row[1]),
            stable_id=str(row[2]),
            kind=str(row[3]),
            file_path=None if row[4] is None else str(row[4]),
            remote_url=None if row[5] is None else str(row[5]),
            updated_at=None if row[6] is None else str(row[6]),
            origin_device_id=None if row[7] is None else str(row[7]),
        )
        for row in conn.execute(
            "SELECT location_id, machine_id, stable_id, kind, file_path, "
            "remote_url, updated_at, origin_device_id FROM track_locations"
        )
    ]


def scan(conn: sqlite3.Connection) -> list[Collapse]:
    """Every natural key holding a non-NFC path, or a duplicate to collapse.

    A table missing from the DB is skipped rather than raising, matching
    :func:`apps.shared.state.normalize_stamps.scan`: this pass runs against
    legacy files.
    """
    if not _table_exists(conn, LOCATIONS_TABLE):
        return []
    groups: dict[tuple[str | None, str, str, str | None, str | None], list[LocationRow]]
    groups = defaultdict(list)
    for row in _read_rows(conn):
        groups[row.normalized_key].append(row)

    collapses: list[Collapse] = []
    for members in groups.values():
        needs_repair = len(members) > 1 or any(not m.stored_is_nfc for m in members)
        if not needs_repair:
            continue
        winner = max(members, key=lambda m: m.lww_key)
        losers = tuple(m for m in members if m.location_id != winner.location_id)
        collapses.append(
            Collapse(
                winner=winner,
                losers=losers,
                nfc_file_path=_nfc(winner.file_path),
                nfc_remote_url=_nfc(winner.remote_url),
            )
        )
    return collapses


def apply_collapses(conn: sqlite3.Connection, collapses: list[Collapse]) -> int:
    """Collapse every group in one transaction. Returns the number repaired.

    Losers are deleted (and their dangling changelog entries pruned) BEFORE
    the survivor's path is rewritten NFC, so the survivor can take the natural
    key its NFC twin used to hold without tripping the partial UNIQUE index
    mid-statement. The survivor is logged carrying its EXISTING ``updated_at``:
    the collapse is not a fresh edit, and bumping the stamp would make the
    survivor win conflicts it should tie or lose.
    """
    if not collapses:
        return 0
    with sync_stamp.stamped_transaction(conn):
        received_at = sync_stamp.canonical_now()
        for collapse in collapses:
            for loser in collapse.losers:
                conn.execute(
                    f"DELETE FROM {LOCATIONS_TABLE} WHERE location_id = ?",
                    (loser.location_id,),
                )
                loser_pk = sync_stamp.encode_row_pk((loser.location_id,))
                for changelog in CHANGELOG_TABLES:
                    conn.execute(
                        f"DELETE FROM {changelog} "
                        f"WHERE table_name = ? AND row_pk = ?",
                        (LOCATIONS_TABLE, loser_pk),
                    )
            conn.execute(
                f"UPDATE {LOCATIONS_TABLE} SET file_path = ?, remote_url = ? "
                f"WHERE location_id = ?",
                (
                    collapse.nfc_file_path,
                    collapse.nfc_remote_url,
                    collapse.winner.location_id,
                ),
            )
            conn.execute(
                f"INSERT INTO {sync_stamp.LOCAL_CHANGELOG_TABLE}("
                "table_name, row_pk, updated_at, origin_device_id, received_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    LOCATIONS_TABLE,
                    sync_stamp.encode_row_pk((collapse.winner.location_id,)),
                    sync_stamp.coalesce_stored_stamp(collapse.winner.updated_at),
                    collapse.winner.origin_device_id or "",
                    received_at,
                ),
            )
    return len(collapses)


def state_db_path(data_dir: Path) -> Path:
    """``<data-dir>/state/state.db`` -- the layout spec D1 fixes."""
    return Path(data_dir) / sync_stamp.STATE_DIR_NAME / "state.db"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m apps.shared.state.normalize_locations",
        description=(
            "Collapse NFD/NFC duplicate track_locations rows and rewrite "
            "stored paths to NFC (round 3 finding R3)."
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
        collapses = scan(conn)
        for collapse in collapses:
            print(collapse.describe())
        if not collapses:
            print("[OK] every track_locations path is NFC; nothing to do")
            return 0
        dropped = sum(len(c.losers) for c in collapses)
        if args.dry_run:
            print(
                f"[DRY-RUN] {len(collapses)} natural key(s) would be repaired "
                f"({dropped} duplicate row(s) dropped)"
            )
            return 0
        repaired = apply_collapses(conn, collapses)
        print(
            f"[OK] repaired {repaired} natural key(s), dropped {dropped} "
            f"duplicate row(s)"
        )
        return 0
    finally:
        conn.close()


__all__ = [
    "CHANGELOG_TABLES",
    "Collapse",
    "LOCATIONS_TABLE",
    "LocationRow",
    "apply_collapses",
    "main",
    "scan",
    "state_db_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
