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
``(stable_id, machine_id, kind, file_path)`` and
``(stable_id, machine_id, kind, remote_url)`` -- exactly the two partial
UNIQUE indexes (schema v6), each in force on its own wherever its columns are
all non-NULL (``apps.sync_hub.protocol_common.NATURAL_KEYS``). This pass
groups rows that share EITHER key AFTER NFC-normalizing the path and URL, so
an NFD row and its NFC twin land in one group even when their other column
differs, and:

* **a lone NFD row** (no twin) has its stored path rewritten to NFC in place;
* **an NFD row plus its NFC twin** collapse to ONE row -- the last-writer-wins
  winner survives (``(updated_at, origin_device_id)``, the same comparison
  ``apps.sync_hub.protocol.lww_key`` makes), the loser is hard-deleted, and
  the survivor's path is stored NFC;
* **a collision chain** (two rows joined only through a third, e.g. one
  shares the path, the other the URL) is left alone and reported: the hub
  resolved it row by row as the rows arrived, so its survivors depend on
  that order, which nothing here records. Only a group whose rows ALL
  collide pairwise ends with the LWW newest alone in every order.

The loser is hard-deleted, not tombstoned, for the reason
``apps.sync_hub.engine_apply._drop_superseded`` gives: a partial UNIQUE index cannot
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
import itertools
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
#: ``apps.sync_hub.engine_apply._drop_superseded``; duplicated rather than imported
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
    def is_orderable(self) -> bool:
        """True when this row's stamp can take part in the LWW comparison.

        A NULL stamp can: ADR 04 c7 reads it as epoch-old, which is a
        decision, not a guess. An unorderable SPELLING cannot -- see
        :func:`scan` for what happens to its group.
        """
        return self.updated_at is None or sync_stamp.is_orderable(self.updated_at)

    @property
    def lww_key(self) -> tuple[str, str]:
        """``(updated_at, origin_device_id)``, the winner-picks-max comparison.

        Only valid on a row where :attr:`is_orderable` is True; callers check
        first. A NULL stamp sorts as :data:`apps.shared.state.sync_stamp.
        EPOCH` (ADR 04 c7) and a NULL origin collapses to ``''``
        (``protocol.NO_ORIGIN``), keeping the order total.
        """
        if self.updated_at is None:
            return (sync_stamp.EPOCH, self.origin_device_id or "")
        return (
            sync_stamp.to_canonical(self.updated_at),
            self.origin_device_id or "",
        )

    @property
    def normalized_keys(self) -> tuple[tuple[str, ...], ...]:
        """Each natural key this row holds, its path or URL NFC-normalized.

        One per partial UNIQUE index whose columns are all non-NULL, as
        ``apps.sync_hub.protocol_common.natural_keys`` reads them: SQLite
        treats NULLs as distinct, so a NULL ``machine_id`` holds neither.
        Two spellings of one file share a key; that is what puts an NFD row
        and its NFC twin in one group.
        """
        if self.machine_id is None:
            return ()
        return tuple(
            (column, self.stable_id, self.machine_id, self.kind, normalized)
            for column, normalized in (
                ("file_path", _nfc(self.file_path)),
                ("remote_url", _nfc(self.remote_url)),
            )
            if normalized is not None
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


def _repairable_groups(
    conn: sqlite3.Connection,
) -> tuple[list[list[LocationRow]], list[list[LocationRow]], list[list[LocationRow]]]:
    """Groups needing repair, split into (orderable, quarantined, chains).

    A group is quarantined when ANY member's stored stamp cannot be ordered.
    Not "the faulted member is dropped from the comparison": this pass picks
    a winner and then HARD-DELETES the losers, so a group resolved without
    one of its members can delete the row carrying the most recent edit. File
    identity would survive (the rows are NFD/NFC twins of one path); edit
    provenance would not, and nothing anywhere would record that it had gone.
    A chain is held for the reason :func:`collision_chains` gives.
    """
    if not _table_exists(conn, LOCATIONS_TABLE):
        return [], [], []
    repairable: list[list[LocationRow]] = []
    quarantined: list[list[LocationRow]] = []
    chains: list[list[LocationRow]] = []
    for members in _colliding_groups(_read_rows(conn)):
        if not (len(members) > 1 or any(not m.stored_is_nfc for m in members)):
            continue
        if not all(member.is_orderable for member in members):
            quarantined.append(members)
        elif not _all_collide_pairwise(members):
            chains.append(members)
        else:
            repairable.append(members)
    return repairable, quarantined, chains


def _all_collide_pairwise(members: list[LocationRow]) -> bool:
    return all(
        set(first.normalized_keys) & set(second.normalized_keys)
        for first, second in itertools.combinations(members, 2)
    )


def _colliding_groups(rows: list[LocationRow]) -> list[list[LocationRow]]:
    """Rows joined by any shared normalized natural key, transitively.

    Union-find over :attr:`LocationRow.normalized_keys`: a row carrying a
    path and a URL can collide with one row on each, and all three must be
    decided together.
    """
    parent = list(range(len(rows)))
    first_holder: dict[tuple[str, ...], int] = {}
    for index, row in enumerate(rows):
        for key in row.normalized_keys:
            holder = first_holder.setdefault(key, index)
            parent[_root(parent, index)] = _root(parent, holder)
    groups: dict[int, list[LocationRow]] = defaultdict(list)
    for index, row in enumerate(rows):
        groups[_root(parent, index)].append(row)
    return list(groups.values())


def _root(parent: list[int], index: int) -> int:
    while parent[index] != index:
        parent[index] = parent[parent[index]]
        index = parent[index]
    return index


def quarantined_groups(conn: sqlite3.Connection) -> list[list[LocationRow]]:
    """Groups this pass REFUSES to collapse, and why they need reporting.

    Every member of one of these needs
    ``python -m apps.shared.state.normalize_stamps --live`` first. Until
    then the duplicate stays, which means its ``track_locations`` digest
    stays divergent -- visible, bounded and reversible by one command, which
    a silently deleted row would not be.
    """
    return _repairable_groups(conn)[1]


def collision_chains(conn: sqlite3.Connection) -> list[list[LocationRow]]:
    """Groups joined only through a third row, which this pass REFUSES to collapse.

    The hub decided such a group row by row as the rows arrived: ``C``,
    ``B``, ``A`` can leave ``A`` alone where ``A`` first leaves ``A`` and
    ``C``. Nothing records that order, so any local rule is a guess that
    either deletes a row the hub kept or keeps one it dropped, and the
    digest stays divergent with no row to say why. Left alone, the group
    stays divergent too, but named: settling it needs the hub's rows
    (``GET /rows?table=track_locations``) and a human decision.
    """
    return _repairable_groups(conn)[2]


def _lww_winner(members: list[LocationRow]) -> LocationRow:
    """The newest stamp; an exact stamp tie keeps the SMALLER ``location_id``.

    The tie rule is the one ``apps.sync_hub.engine_apply._duplicate_incoming_wins``
    applies to the same two rows, so this spoke elects the survivor the hub
    already kept. ``max`` alone would keep whichever tied row SQLite returned
    first, delete the hub's survivor, and re-offer the row the hub dropped.
    """
    newest = max(member.lww_key for member in members)
    return min(
        (member for member in members if member.lww_key == newest),
        key=lambda member: member.location_id,
    )


def scan(conn: sqlite3.Connection) -> list[Collapse]:
    """Every natural key holding a non-NFC path, or a duplicate to collapse.

    A table missing from the DB is skipped rather than raising, matching
    :func:`apps.shared.state.normalize_stamps.scan`: this pass runs against
    legacy files. A group holding an unorderable stored stamp is NOT here --
    it is reported by :func:`quarantined_groups` instead, and a collision
    chain by :func:`collision_chains`.
    """
    collapses: list[Collapse] = []
    for members in _repairable_groups(conn)[0]:
        winner = _lww_winner(members)
        collapses.append(
            Collapse(
                winner=winner,
                losers=tuple(m for m in members if m.location_id != winner.location_id),
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
        _collapse_in_transaction(conn, collapses)
    return len(collapses)


def collapse_all(conn: sqlite3.Connection) -> list[Collapse]:
    """Scan AND collapse under one write lock; returns what was collapsed.

    :func:`scan` then :func:`apply_collapses` leaves a gap: a writer that
    commits a newer stamp onto a planned loser between the two has its edit
    hard-deleted on a decision made before it existed. ``BEGIN IMMEDIATE``
    takes the write lock before the rows are read, so the winner is elected
    over exactly the rows the delete sees. The automatic pre-offer repair in
    ``apps.sync_hub.client_recovery`` runs this, not the two-step form.
    """
    with sync_stamp.stamped_transaction(conn):
        collapses = scan(conn)
        _collapse_in_transaction(conn, collapses)
    return collapses


def _collapse_in_transaction(conn: sqlite3.Connection, collapses: list[Collapse]) -> None:
    """The deletes and rewrites of :func:`apply_collapses`; caller holds the lock."""
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
                # VERBATIM (round 5). Nothing orders
                # local_changelog.updated_at -- the push fence reads
                # (seq, table_name, row_pk) -- so the honest value is the
                # row's own. A NULL has no verbatim value and the column
                # is NOT NULL, so it takes the storable floor.
                (
                    sync_stamp.FLOOR_STAMP
                    if collapse.winner.updated_at is None
                    else collapse.winner.updated_at
                ),
                collapse.winner.origin_device_id or "",
                received_at,
            ),
        )

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


#: Exit code when the safe repairs ran but some group could not be touched
#: because a member's stored stamp is unorderable, or because it is a collision
#: chain (:func:`collision_chains`). NOT 0: a partial repair
#: reported as success is the "clean binary answer for a case that should be
#: messy" .claude/rules/verification.md names, and the operator has one more
#: command to run before this pass can finish its job.
EXIT_QUARANTINED: int = 3


def _report_quarantined(groups: list[list[LocationRow]]) -> None:
    for members in groups:
        first = members[0]
        offending = [m for m in members if not m.is_orderable]
        print(
            f"[QUARANTINED] {LOCATIONS_TABLE} {first.machine_id}/"
            f"{first.stable_id}/{first.kind}: "
            + ", ".join(f"{m.location_id} updated_at={m.updated_at!r}" for m in offending)
            + " cannot be ordered; not collapsed",
            file=sys.stderr,
        )
    print(
        f"[WARN] {len(groups)} natural key(s) skipped: run `python -m "
        f"apps.shared.state.normalize_stamps --live` first, then re-run this "
        f"pass",
        file=sys.stderr,
    )


def _report_chains(groups: list[list[LocationRow]]) -> None:
    for members in groups:
        first = members[0]
        print(
            f"[CHAIN] {LOCATIONS_TABLE} {first.machine_id}/{first.stable_id}/"
            f"{first.kind}: " + ", ".join(m.location_id for m in members)
            + " collide only through one another; not collapsed",
            file=sys.stderr,
        )
    print(
        f"[WARN] {len(groups)} collision chain(s) skipped: their survivors depend "
        f"on the order the hub received them. Compare with the hub's rows and "
        f"delete the ones it does not hold",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    target = state_db_path(args.data_dir)
    if not target.exists():
        print(f"[ERROR] no state DB at {target}", file=sys.stderr)
        return 2
    conn = state_db.open_rw(target)
    try:
        collapses = scan(conn)
        held_back = quarantined_groups(conn)
        chains = collision_chains(conn)
        for collapse in collapses:
            print(collapse.describe())
        if held_back:
            _report_quarantined(held_back)
        if chains:
            _report_chains(chains)
        unfinished = EXIT_QUARANTINED if held_back or chains else 0
        if not collapses:
            print("[OK] every track_locations path is NFC; nothing to do")
            return unfinished
        dropped = sum(len(c.losers) for c in collapses)
        if args.dry_run:
            print(
                f"[DRY-RUN] {len(collapses)} natural key(s) would be repaired "
                f"({dropped} duplicate row(s) dropped)"
            )
            return unfinished
        repaired = apply_collapses(conn, collapses)
        print(
            f"[OK] repaired {repaired} natural key(s), dropped {dropped} "
            f"duplicate row(s)"
        )
        return unfinished
    finally:
        conn.close()


__all__ = [
    "CHANGELOG_TABLES",
    "EXIT_QUARANTINED",
    "LOCATIONS_TABLE",
    "Collapse",
    "LocationRow",
    "apply_collapses",
    "collapse_all",
    "collision_chains",
    "main",
    "quarantined_groups",
    "scan",
    "state_db_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
