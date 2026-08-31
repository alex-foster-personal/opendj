"""Merge engine: push selection, last-writer-wins apply, changelog, watermarks.

Contract: ``specs/design_decision_04.md``. Nothing in this module stamps a
timestamp onto a domain row -- rows carry the ``updated_at`` /
``origin_device_id`` their originating machine wrote, and that pair is the
only thing the merge looks at. The single clock this module reads is
``hub_changelog.received_at``, which is hub bookkeeping and never syncs.

Four behaviours that look surprising until you know why:

* **Push selection is a sequence fence, never a clock** (ADR 08 point 3,
  round 1 finding 3). A spoke offers the rows its own ``local_changelog``
  recorded above ``sync_state.last_push_seq``. The round 1 push floor was a
  wall clock maximum taken over rows the machine had just *pulled*, so a
  single peer with a skewed clock could raise the floor past every local
  edit and those edits were never offered again -- not rejected, never sent.
  ``updated_at`` now resolves conflicts and nothing else.
* **A spoke that has never completed a sync against this peer offers
  everything.** Migration v6 cannot retro-log the rows that already existed,
  so there is no changelog entry to fence against; ``last_sync_at`` records
  that a full offer has happened at least once. It is a completion marker,
  not a watermark: nothing ever compares it to a row's ``updated_at``.
* **``track_locations`` resolves on its natural key, not its primary key**
  (ADR 08 point 1). ``location_id`` is a random uuid minted per machine, so
  the same logical row can arrive under a key this DB has never seen; keying
  the upsert on ``location_id`` alone turned that into a UNIQUE violation,
  an HTTP 409, and a spoke that could never sync again (finding 1). The
  duplicate is resolved by LWW like any other conflict, with the smaller
  ``location_id`` as the tiebreak so both peers converge on the same one.
* **A losing playlist row discards its membership bundle.** Membership is
  whole-playlist (ADR 04 c5): it replaces the peer's copy only when the
  ``playlists`` row itself wins, so a reorder and an add in the same window
  resolve to one of them, not to an interleaving of both.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from apps.shared.state import sync_stamp
from apps.shared.state.sync_stamp import LOCAL_CHANGELOG_TABLE
from apps.sync_hub import protocol
from apps.sync_hub.protocol import (
    MEMBERSHIP_TABLE,
    SPEC_BY_TABLE,
    SYNC_TABLES,
    MachineRow,
    RowChange,
    TableSpec,
)

log = logging.getLogger(__name__)

HUB_CHANGELOG_TABLE: str = "hub_changelog"

#: The two changelog tables :func:`prune_changelog` will touch. An allowlist
#: because the table name is interpolated into the DELETE -- SQLite does not
#: bind identifiers, so this check is load-bearing, not decorative.
CHANGELOG_TABLES: frozenset[str] = frozenset({"hub_changelog", LOCAL_CHANGELOG_TABLE})

#: Retention defaults for :func:`prune_changelog`. Generous on purpose: the
#: prune is lossless for the pull (it only drops superseded entries), so the
#: bounds exist to keep a recent audit trail, not to protect correctness.
DEFAULT_KEEP_DAYS: float = 30.0
DEFAULT_KEEP_ROWS: int = 10_000

#: Hub-side cap on one ``/pull`` response. A first sync of a real library is
#: 6.6 MB+ of JSON (round 1 finding A2), held twice in memory on each side;
#: the client loops until the hub reports no more.
DEFAULT_PULL_LIMIT: int = 500


class SyncApplyError(RuntimeError):
    """A row could not be applied. Loud by design; sync stops here."""


class SyncSchemaMismatch(SyncApplyError):
    """A peer offered a row whose columns are not this DB's columns."""


_APPLY_ORDER: dict[str, int] = {
    spec.name: index for index, spec in enumerate(SYNC_TABLES)
}


# ----- watermarks (machine-local ``sync_state``) ---------------------------


@dataclass(frozen=True)
class Watermark:
    """This machine's position against one peer. Machine-local, never synced.

    * ``last_push_seq`` is the greatest ``local_changelog.seq`` this machine
      has already offered to ``peer``. The push fence (ADR 08 point 3).
    * ``last_pull_seq`` is the greatest ``hub_changelog.seq`` already applied
      from ``peer``.
    * ``last_sync_at`` records that a sync against ``peer`` has completed at
      least once, and when. It is an operator readout and the "already
      seeded" flag behind :attr:`needs_full_offer` -- deliberately NOT a
      watermark. Round 1 lost rows precisely because a wall clock was one
      (finding 3), so nothing here compares it against a row's
      ``updated_at``.
    * ``peer_generation`` is the token ``peer`` reported at the last
      completed sync (:mod:`apps.sync_hub.generation`). A different token
      next time means that peer's DB moved backwards and everything above is
      meaningless, which is round 2's replacement for inferring a restore
      from the peer's ``MAX(seq)`` (finding N6). None until one sync
      completes.
    """

    peer: str
    last_push_seq: int = 0
    last_pull_seq: int = 0
    last_sync_at: str | None = None
    peer_generation: str | None = None

    @property
    def needs_full_offer(self) -> bool:
        """True until one sync against this peer has completed.

        Rows that predate ``local_changelog`` (anything migrated in from v5,
        which on a real library is the whole library) have no changelog entry
        to fence against, so the first offer has to be a full scan. It is
        also what a hub-restore reset falls back to (ADR 08 point 4).
        """
        return self.last_sync_at is None


def read_watermark(conn: sqlite3.Connection, peer: str) -> Watermark:
    """Load the ``sync_state`` row for ``peer``; zeros if never synced."""
    row = conn.execute(
        "SELECT last_push_seq, last_pull_seq, last_sync_at, peer_generation "
        "FROM sync_state WHERE peer = ?",
        (peer,),
    ).fetchone()
    if row is None:
        return Watermark(peer=peer)
    return Watermark(
        peer=peer,
        last_push_seq=int(row[0]),
        last_pull_seq=int(row[1]),
        last_sync_at=None if row[2] is None else str(row[2]),
        peer_generation=None if row[3] is None else str(row[3]),
    )


def write_watermark(conn: sqlite3.Connection, watermark: Watermark) -> None:
    """Upsert one ``sync_state`` row. Machine-local; never synced."""
    conn.execute(
        """
        INSERT INTO sync_state(
            peer, last_push_seq, last_pull_seq, last_sync_at, peer_generation
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(peer) DO UPDATE SET
            last_push_seq   = excluded.last_push_seq,
            last_pull_seq   = excluded.last_pull_seq,
            last_sync_at    = excluded.last_sync_at,
            peer_generation = excluded.peer_generation
        """,
        (
            watermark.peer,
            watermark.last_push_seq,
            watermark.last_pull_seq,
            watermark.last_sync_at,
            watermark.peer_generation,
        ),
    )


# ----- sequences -----------------------------------------------------------


def current_seq(conn: sqlite3.Connection) -> int:
    """Greatest ``hub_changelog.seq``; 0 on a machine that has never been a hub."""
    row = conn.execute(
        f"SELECT COALESCE(MAX(seq), 0) FROM {HUB_CHANGELOG_TABLE}"
    ).fetchone()
    return 0 if row is None else int(row[0])


def local_seq(conn: sqlite3.Connection) -> int:
    """Greatest ``local_changelog.seq``: this machine's own write counter.

    Read BEFORE selecting the rows to offer, and stored as the new
    ``last_push_seq`` only after the push lands. A local write that commits
    during the round trip therefore lands ABOVE the recorded fence and is
    offered on the next sync, instead of falling into the gap that lost rows
    in round 1.
    """
    row = conn.execute(
        f"SELECT COALESCE(MAX(seq), 0) FROM {LOCAL_CHANGELOG_TABLE}"
    ).fetchone()
    return 0 if row is None else int(row[0])


# ----- machines registry ---------------------------------------------------


def upsert_machine(conn: sqlite3.Connection, machine: MachineRow) -> bool:
    """Insert or overwrite one machine's OWN row unconditionally. True if changed.

    This is the write a machine makes about ITSELF: the owner-scoped branch of
    :func:`merge_machines`, driven by the ``machine_id`` that authored the
    snapshot. Its owner is authoritative about its own
    ``name`` / ``platform`` / ``is_hub`` / ``data_root``, so there is no LWW
    and -- since round 3 finding R1 -- no ``last_seen`` wall-clock gate. That
    gate was the write primitive: it let any peer with a fast clock rewrite
    every OTHER machine's registry row, because ``last_seen`` is stamped from
    the local wall clock and nothing checked that the pusher WAS the machine
    it described. Every other machine's row now takes the
    :func:`_insert_machine_if_absent` path instead, so this unconditional
    overwrite only ever touches the caller's own row.
    """
    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO machines(
            machine_id, name, platform, is_hub, data_root, first_seen, last_seen
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(machine_id) DO UPDATE SET
            name      = excluded.name,
            platform  = excluded.platform,
            is_hub    = excluded.is_hub,
            data_root = excluded.data_root,
            last_seen = excluded.last_seen
        """,
        (
            machine.machine_id,
            machine.name,
            machine.platform,
            1 if machine.is_hub else 0,
            machine.data_root,
            machine.first_seen,
            machine.last_seen,
        ),
    )
    return conn.total_changes > before


def _insert_machine_if_absent(
    conn: sqlite3.Connection, machine: MachineRow
) -> bool:
    """Create a peer's row if this DB has never met it; never rewrite one.

    A snapshot from a peer is allowed to TEACH this DB about machines it has
    not met -- the FK parents from ``sync_policies`` / ``playlist_pins`` /
    ``track_locations`` must land, which is the round 2 finding N4 fix and has
    to survive -- but it must not be able to REWRITE a row it does not own
    (round 3 finding R1). ``INSERT OR IGNORE`` is exactly that: it inserts an
    unknown machine and does nothing for a known one. It also swallows a
    ``machines.name`` UNIQUE collision arriving in the snapshot (round 3
    finding R1a): the machine such a forged name would lock out is not the one
    that sent the snapshot, so the row is simply not applied rather than
    raising out of ``hello`` / ``push`` and bricking an innocent third machine.
    """
    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO machines(
            machine_id, name, platform, is_hub, data_root, first_seen, last_seen
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            machine.machine_id,
            machine.name,
            machine.platform,
            1 if machine.is_hub else 0,
            machine.data_root,
            machine.first_seen,
            machine.last_seen,
        ),
    )
    return conn.total_changes > before


def machines_snapshot(conn: sqlite3.Connection) -> list[MachineRow]:
    """Every known machine, ordered by id so the payload is deterministic."""
    cursor = conn.execute(
        "SELECT machine_id, name, platform, is_hub, data_root, first_seen, last_seen "
        "FROM machines ORDER BY machine_id"
    )
    return [
        MachineRow(
            machine_id=str(row[0]),
            name=str(row[1]),
            platform=str(row[2]),
            is_hub=bool(row[3]),
            data_root=None if row[4] is None else str(row[4]),
            first_seen=str(row[5]),
            last_seen=str(row[6]),
        )
        for row in cursor
    ]


def merge_machines(
    conn: sqlite3.Connection, machines: Iterable[MachineRow], *, caller_id: str
) -> int:
    """Apply a registry snapshot under owner-scoped rules. Returns rows changed.

    ``caller_id`` is the machine that AUTHORED this snapshot -- the pusher on
    ``push``, the spoke on its ``hello``, the hub on the snapshot a spoke
    pulls. That machine is authoritative about ITS OWN row, so its row is
    upserted (:func:`upsert_machine`); every OTHER row is
    :func:`_insert_machine_if_absent`, created only if this DB has never met
    it and never rewritten. That is the round 3 finding R1 fix: the old merge
    let a peer's snapshot rewrite any machine's ``name`` / ``platform`` /
    ``is_hub`` / ``data_root`` under a bare ``last_seen`` wall-clock gate, so
    one forged push could rename a victim, and a forged ``name`` collision
    could raise out of a THIRD machine's own ``hello`` and lock it out (R1a).

    A ``machines.name`` collision on the CALLER'S OWN row is a different
    matter -- two machines genuinely sharing a hostname (finding A5) -- and
    still raises :class:`SyncApplyError`, because the machine that hits it is
    the one that caused it.
    """
    changed = 0
    for machine in machines:
        if machine.machine_id == caller_id:
            try:
                if upsert_machine(conn, machine):
                    changed += 1
            except sqlite3.IntegrityError as exc:
                raise SyncApplyError(
                    f"machines row {machine.machine_id} ({machine.name!r}) "
                    f"conflicts with a local row: {exc}. machines.name is "
                    f"UNIQUE, so two machines sharing a hostname collide here "
                    f"rather than silently overwriting each other."
                ) from exc
        elif _insert_machine_if_absent(conn, machine):
            changed += 1
    return changed


# ----- push ----------------------------------------------------------------


def _members_for_playlist(
    conn: sqlite3.Connection, playlist_id: str
) -> tuple[dict[str, Any], ...]:
    columns = protocol.table_columns(conn, MEMBERSHIP_TABLE)
    cursor = conn.execute(
        f"SELECT {', '.join(columns)} FROM {MEMBERSHIP_TABLE} "
        f"WHERE playlist_id = ? ORDER BY position",
        (playlist_id,),
    )
    return tuple(
        protocol.canonical_row(MEMBERSHIP_TABLE, columns, row) for row in cursor
    )


def _row_change(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    spec: TableSpec,
    row: Sequence[Any],
) -> RowChange:
    values = protocol.canonical_row(table, columns, row)
    pk = tuple(str(values[column]) for column in spec.pk)
    members = _members_for_playlist(conn, pk[0]) if table == "playlists" else None
    return RowChange(table=table, pk=pk, values=values, members=members)


def _rows_for_table(conn: sqlite3.Connection, spec: TableSpec) -> list[RowChange]:
    columns = protocol.table_columns(conn, spec.name)
    order_by = ", ".join(spec.pk)
    cursor = conn.execute(
        f"SELECT {', '.join(columns)} FROM {spec.name} ORDER BY {order_by}"
    )
    return [_row_change(conn, spec.name, columns, spec, row) for row in cursor]


def _changelog_rows(
    conn: sqlite3.Connection,
    entries: Sequence[tuple[Any, Any, Any]],
    *,
    changelog: str,
) -> tuple[list[RowChange], int]:
    """Read the current state of every row named by ``entries``.

    A changelog records THAT a row changed, not what it looked like, so each
    row is read live and several entries for one row collapse to a single
    change carrying its newest content -- the same thing LWW would have
    converged on, at a fraction of the payload.

    A ``playlist_memberships`` entry resolves to its ``playlists`` row. A
    writer that edits membership logs the membership rows it touched -- it
    has to, or the fence could not see the write at all -- but membership
    never travels on its own (ADR 04 c5): the parent row carries the whole
    bundle. Resolving it here rather than at the writer keeps that rule in
    one place, and it holds even for a writer that forgot to bump
    ``playlists.updated_at`` (ADR 08 point 8b, still an open question).

    Rows are returned in ``_APPLY_ORDER`` (parents before children), which
    the batching in :mod:`apps.sync_hub.client` depends on: a chunk boundary
    must never put a child row in an earlier request than its parent.

    Returns the rows and the number of entries SKIPPED because the row they
    name no longer exists (round 2 finding 4a; see the comment at the skip).
    """
    latest: dict[tuple[str, str], int] = {}
    for seq, table_name, row_pk in entries:
        table = str(table_name)
        key = str(row_pk)
        if table == MEMBERSHIP_TABLE:
            playlist_id = protocol.decode_row_pk(key)[0]
            if playlist_id is None:
                raise SyncApplyError(
                    f"{changelog} has a {MEMBERSHIP_TABLE} entry with a NULL "
                    f"playlist_id ({key}); it cannot name the row that carries "
                    f"it."
                )
            table = "playlists"
            key = protocol.encode_row_pk((playlist_id,))
        latest[(table, key)] = int(seq)

    changes: list[RowChange] = []
    skipped = 0
    for table_name, row_pk in latest:
        spec = SPEC_BY_TABLE.get(table_name)
        if spec is None:
            raise SyncApplyError(
                f"{changelog} references table {table_name!r}, which is not "
                f"in the sync set"
            )
        pk = protocol.decode_row_pk(row_pk)
        columns = protocol.table_columns(conn, table_name)
        row = conn.execute(
            f"SELECT {', '.join(columns)} FROM {table_name} "
            f"WHERE {_pk_predicate(spec)}",
            tuple(pk),
        ).fetchone()
        if row is None:
            # Round 2 finding 4a's blast radius. Raising here answered EVERY
            # pull with a 409 for as long as the entry existed, so one manual
            # DELETE on the hub took the whole fleet offline with no repair
            # path. The entry is bookkeeping and the row it names is gone;
            # there is nothing to serialize and nothing a peer could do with
            # it. Skip it, loudly. A soft delete is NOT this case: a
            # tombstoned row still exists and still travels.
            skipped += 1
            log.error(
                "%s points at %s row %s which no longer exists; skipping the "
                "entry. Deletes in the sync set are soft (ADR 04 c4), so "
                "something hard-deleted a synced row -- that row will not "
                "reach any peer and the digest will say so.",
                changelog,
                table_name,
                row_pk,
            )
            continue
        changes.append(_row_change(conn, table_name, columns, spec, row))
    changes.sort(key=lambda change: (_APPLY_ORDER[change.table], change.pk))
    return changes, skipped


def spoke_push(
    conn: sqlite3.Connection,
    *,
    watermark: Watermark | None = None,
    ceiling: int | None = None,
) -> list[RowChange]:
    """The rows this machine offers its peer.

    Two selections, and which one runs is the whole of ADR 08 point 3:

    * **full offer** when no sync against this peer has completed yet -- the
      library predates ``local_changelog``, so a fence would offer nothing.
    * **fenced offer** afterwards: exactly the rows ``local_changelog``
      recorded in ``(last_push_seq, ceiling]``. ``ceiling`` is
      :func:`local_seq` read before selection, so a write that commits during
      the round trip lands above the fence rather than in a gap.

    ``playlist_memberships`` never appears as a top-level change -- each
    ``playlists`` row carries its complete membership bundle instead
    (ADR 04 c5).
    """
    if watermark is None or watermark.needs_full_offer:
        changes: list[RowChange] = []
        for spec in SYNC_TABLES:
            changes.extend(_rows_for_table(conn, spec))
        return changes
    top = local_seq(conn) if ceiling is None else int(ceiling)
    entries = conn.execute(
        f"SELECT seq, table_name, row_pk FROM {LOCAL_CHANGELOG_TABLE} "
        f"WHERE seq > ? AND seq <= ? ORDER BY seq",
        (watermark.last_push_seq, top),
    ).fetchall()
    changes, _skipped = _changelog_rows(
        conn, entries, changelog=LOCAL_CHANGELOG_TABLE
    )
    return changes


# ----- apply ---------------------------------------------------------------


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of applying a batch of offered rows."""

    accepted: int
    rejected: int
    seq: int


def _pk_predicate(spec: TableSpec) -> str:
    return " AND ".join(f"{column} = ?" for column in spec.pk)


def _stored_sort_key(
    conn: sqlite3.Connection, spec: TableSpec, pk: Sequence[str]
) -> tuple[str, str] | None:
    row = conn.execute(
        f"SELECT {protocol.UPDATED_AT}, {protocol.ORIGIN_DEVICE_ID} "
        f"FROM {spec.name} WHERE {_pk_predicate(spec)}",
        tuple(pk),
    ).fetchone()
    if row is None:
        return None
    return protocol.lww_key(
        {protocol.UPDATED_AT: row[0], protocol.ORIGIN_DEVICE_ID: row[1]}
    )


def _natural_conflict_pks(
    conn: sqlite3.Connection, spec: TableSpec, change: RowChange
) -> tuple[tuple[str, ...], ...]:
    """Primary keys of DIFFERENT local rows holding this row's natural keys.

    Empty when the table has no natural key, when nothing local holds one, or
    when the row that holds it IS this row. Anything else is the round 1
    finding 1 shape: one logical row, two ``location_id`` values, and a
    partial UNIQUE index that will not let both exist.

    One lookup per applicable index, not one for the first of them (round 2
    finding N5). A row carrying both ``file_path`` and ``remote_url`` can
    collide with one local row on the path index and a DIFFERENT local row
    on the url index; resolving the first and inserting left the second to
    raise ``UNIQUE constraint failed`` from inside the apply, which is a 409
    no retry could ever get past.
    """
    found: list[tuple[str, ...]] = []
    for key in protocol.natural_keys(change.table, change.values):
        predicate = " AND ".join(f"{column} = ?" for column, _ in key)
        row = conn.execute(
            f"SELECT {', '.join(spec.pk)} FROM {change.table} WHERE {predicate}",
            tuple(value for _, value in key),
        ).fetchone()
        if row is None:
            continue
        conflict = tuple(str(value) for value in row)
        if conflict != change.pk and conflict not in found:
            found.append(conflict)
    return tuple(found)


def _duplicate_sort_key(
    conn: sqlite3.Connection,
    spec: TableSpec,
    change: RowChange,
    conflict_pk: Sequence[str],
) -> tuple[str, str]:
    """Sort key of the local duplicate at ``conflict_pk``. Never None.

    A row that holds another row's natural key but carries no sort key was
    written by something that bypassed the sync columns entirely. The merge
    cannot order it against anything, and guessing is how a real edit gets
    overwritten by a row nothing can date.
    """
    stored = _stored_sort_key(conn, spec, conflict_pk)
    if stored is None:
        raise SyncApplyError(
            f"{change.table}: row {list(conflict_pk)} holds the natural key of "
            f"{list(change.pk)} but has no sort key; the table was written by "
            f"something that bypassed the sync columns."
        )
    return stored


def _duplicate_incoming_wins(
    change: RowChange, stored: tuple[str, str], conflict_pk: tuple[str, ...]
) -> bool:
    """LWW between two rows that share a natural key under different pks.

    The tie is broken on the primary key itself rather than on arrival
    order: both peers evaluate the same rule over the same two rows, so both
    converge on the same survivor. Without it, two rows with identical
    ``(updated_at, origin_device_id)`` reject each other forever and the
    digest never matches (the shape of round 1 finding 5b).
    """
    if change.sort_key != stored:
        return change.sort_key > stored
    return change.pk < conflict_pk


def _drop_superseded(
    conn: sqlite3.Connection, spec: TableSpec, pk: Sequence[str]
) -> None:
    """Remove the losing duplicate, and every changelog entry naming it.

    The row is hard-deleted rather than tombstoned because the partial
    UNIQUE index would refuse to hold a tombstone and its replacement at
    once. Pruning the changelogs is what keeps that safe: a dangling entry
    would make ``hub_changes_since`` raise for EVERY spoke (round 1 finding
    4a's blast radius), and a dangling ``local_changelog`` entry would do the
    same to this machine's next push. Peers still converge without a
    tombstone -- they receive the winner, resolve it against their own copy
    on the same natural key, and drop their duplicate by this same path.
    """
    row_pk = protocol.encode_row_pk(pk)
    conn.execute(
        f"DELETE FROM {spec.name} WHERE {_pk_predicate(spec)}", tuple(pk)
    )
    for changelog in (HUB_CHANGELOG_TABLE, LOCAL_CHANGELOG_TABLE):
        conn.execute(
            f"DELETE FROM {changelog} WHERE table_name = ? AND row_pk = ?",
            (spec.name, row_pk),
        )


def _checked_values(
    conn: sqlite3.Connection, table: str, spec: TableSpec, change: RowChange
) -> tuple[tuple[str, ...], list[Any]]:
    """Validate the offered row against the local table; return columns+values."""
    columns = protocol.table_columns(conn, table)
    offered = set(change.values)
    expected = set(columns)
    if offered != expected:
        missing = sorted(expected - offered)
        extra = sorted(offered - expected)
        raise SyncSchemaMismatch(
            f"{table}: peer row does not match this schema "
            f"(missing={missing}, unexpected={extra}). Both machines must be "
            f"on the same apps.shared.state.schema.SCHEMA_VERSION."
        )
    for index, column in enumerate(spec.pk):
        declared = change.pk[index]
        embedded = change.values[column]
        if embedded is None or str(embedded) != declared:
            raise SyncApplyError(
                f"{table}: declared pk {declared!r} for column {column!r} does "
                f"not match the row's own value {embedded!r}"
            )
    return columns, [change.values[column] for column in columns]


def _upsert(
    conn: sqlite3.Connection,
    table: str,
    spec: TableSpec,
    columns: Sequence[str],
    values: Sequence[Any],
) -> None:
    updatable = [column for column in columns if column not in spec.pk]
    assignments = ", ".join(f"{column} = excluded.{column}" for column in updatable)
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT({', '.join(spec.pk)}) DO UPDATE SET {assignments}"
    )
    try:
        conn.execute(sql, tuple(values))
    except sqlite3.IntegrityError as exc:
        raise SyncApplyError(
            f"{table}: applying row {list(values)[:len(spec.pk)]} violates a "
            f"constraint ({exc}). Either the referenced parent row has not "
            f"reached this machine yet, or a UNIQUE index disagrees with the "
            f"peer's copy."
        ) from exc


def _replace_members(
    conn: sqlite3.Connection, playlist_id: str, members: Sequence[dict[str, Any]]
) -> None:
    """Whole-playlist replace (ADR 04 c5). Runs only when the playlist won."""
    columns = protocol.table_columns(conn, MEMBERSHIP_TABLE)
    conn.execute(f"DELETE FROM {MEMBERSHIP_TABLE} WHERE playlist_id = ?", (playlist_id,))
    sql = (
        f"INSERT INTO {MEMBERSHIP_TABLE} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})"
    )
    for member in members:
        offered = set(member)
        if offered != set(columns):
            raise SyncSchemaMismatch(
                f"{MEMBERSHIP_TABLE}: membership row for playlist "
                f"{playlist_id} does not match this schema "
                f"(missing={sorted(set(columns) - offered)}, "
                f"unexpected={sorted(offered - set(columns))})"
            )
        if str(member["playlist_id"]) != playlist_id:
            raise SyncApplyError(
                f"{MEMBERSHIP_TABLE}: bundle for playlist {playlist_id} carries "
                f"a row belonging to {member['playlist_id']!r}"
            )
        try:
            conn.execute(sql, tuple(member[column] for column in columns))
        except sqlite3.IntegrityError as exc:
            raise SyncApplyError(
                f"{MEMBERSHIP_TABLE}: playlist {playlist_id} position "
                f"{member.get('position')!r} references a track this machine "
                f"does not have yet ({exc})"
            ) from exc


def _apply(
    conn: sqlite3.Connection,
    changes: Sequence[RowChange],
    *,
    record_changelog: bool,
    received_at: str | None = None,
) -> ApplyResult:
    # canonical_now(), not a local isoformat() call (round 3 finding R8): the
    # two disagree on a zero-microsecond tick, where isoformat() omits the
    # field and the bare +00:00 then sorts BELOW a canonical .000000+00:00
    # stamp naming the same instant -- hub_changelog.received_at would look a
    # fraction of a second older than it really is.
    stamp = received_at or sync_stamp.canonical_now()
    ordered = sorted(changes, key=lambda change: _APPLY_ORDER[change.table])
    accepted = 0
    rejected = 0
    for change in ordered:
        spec = SPEC_BY_TABLE.get(change.table)
        if spec is None:
            raise SyncApplyError(f"{change.table!r} is not in the sync set")
        columns, values = _checked_values(conn, change.table, spec, change)
        if _loses_to_a_stored_row(conn, spec, change):
            rejected += 1
            continue
        _upsert(conn, change.table, spec, columns, values)
        if change.table == "playlists" and change.members is not None:
            _replace_members(conn, change.pk[0], change.members)
        if record_changelog:
            _log_hub_change(conn, change, stamp)
        accepted += 1
    return ApplyResult(accepted=accepted, rejected=rejected, seq=current_seq(conn))


def _loses_to_a_stored_row(
    conn: sqlite3.Connection, spec: TableSpec, change: RowChange
) -> bool:
    """True if ``change`` is stale against what is already stored.

    Drops any duplicate it beats along the way as a side effect. Split out
    of :func:`_apply` to keep its own branch count under the quality-gate
    mccabe limit; the two paths are exactly what the loop body inlined
    before: no natural-key collision means a plain LWW compare against the
    primary key, a collision means beating EVERY duplicate it collides with.
    """
    conflict_pks = _natural_conflict_pks(conn, spec, change)
    if not conflict_pks:
        stored = _stored_sort_key(conn, spec, change.pk)
        return stored is not None and change.sort_key <= stored
    # The incoming row has to beat EVERY duplicate it collides with; losing
    # to one of them means the row it lost to is the survivor and this one
    # is the stale copy.
    if not all(
        _duplicate_incoming_wins(
            change, _duplicate_sort_key(conn, spec, change, pk), pk
        )
        for pk in conflict_pks
    ):
        return True
    for conflict_pk in conflict_pks:
        _drop_superseded(conn, spec, conflict_pk)
    return False


def _log_hub_change(conn: sqlite3.Connection, change: RowChange, stamp: str) -> None:
    """One ``hub_changelog`` append for an accepted row. Split out of
    :func:`_apply` for the same reason as :func:`_loses_to_a_stored_row`."""
    conn.execute(
        """
        INSERT INTO hub_changelog(
            table_name, row_pk, updated_at, origin_device_id, received_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (change.table, change.row_pk, change.updated_at, change.origin_device_id, stamp),
    )


def hub_apply(
    conn: sqlite3.Connection,
    changes: Sequence[RowChange],
    *,
    received_at: str | None = None,
) -> ApplyResult:
    """Merge a spoke's push into the hub DB and append to ``hub_changelog``."""
    return _apply(conn, changes, record_changelog=True, received_at=received_at)


def spoke_apply(
    conn: sqlite3.Connection, changes: Sequence[RowChange]
) -> ApplyResult:
    """Merge a hub pull into a spoke DB. Same LWW rule, no changelog.

    A spoke keeps no changelog: its pull watermark is the hub's ``seq``, so a
    local changelog would only be a second, divergent numbering.
    """
    return _apply(conn, changes, record_changelog=False)


# ----- pull ----------------------------------------------------------------


@dataclass(frozen=True)
class ChangeBatch:
    """Rows a spoke should apply, the seq that consumed them, and whether
    the hub still holds entries above that seq.

    ``skipped`` counts entries whose row is gone from the hub (round 2
    finding 4a). Reported rather than inferred so an operator sees the number
    without reading the hub's log.
    """

    rows: list[RowChange]
    seq: int
    has_more: bool = False
    skipped: int = 0


def hub_changes_since(
    conn: sqlite3.Connection, since_seq: int, *, limit: int | None = None
) -> ChangeBatch:
    """One chunk of the rows touched after ``since_seq``, read live.

    ``limit`` caps the number of CHANGELOG ENTRIES consumed, not the number
    of rows returned, so :attr:`ChangeBatch.seq` is always a resume point the
    caller can hand back verbatim -- deduping first and then cutting would
    leave entries below the reported seq unsent. Entries stay in seq order,
    so a client that applies chunk after chunk sees parents before children.
    """
    sql = (
        f"SELECT seq, table_name, row_pk FROM {HUB_CHANGELOG_TABLE} "
        f"WHERE seq > ? ORDER BY seq"
    )
    params: list[Any] = [int(since_seq)]
    if limit is not None:
        if limit < 1:
            raise SyncApplyError(f"pull limit must be >= 1, got {limit}")
        sql += " LIMIT ?"
        params.append(int(limit))
    entries = conn.execute(sql, tuple(params)).fetchall()
    max_seq = max((int(entry[0]) for entry in entries), default=int(since_seq))
    remaining = conn.execute(
        f"SELECT 1 FROM {HUB_CHANGELOG_TABLE} WHERE seq > ? LIMIT 1", (max_seq,)
    ).fetchone()
    rows, skipped = _changelog_rows(conn, entries, changelog=HUB_CHANGELOG_TABLE)
    return ChangeBatch(
        rows=rows,
        seq=max_seq,
        has_more=remaining is not None,
        skipped=skipped,
    )


# ----- retention -----------------------------------------------------------


def prune_changelog(
    conn: sqlite3.Connection,
    *,
    changelog: str = HUB_CHANGELOG_TABLE,
    keep_days: float = DEFAULT_KEEP_DAYS,
    keep_rows: int = DEFAULT_KEEP_ROWS,
    now: str | None = None,
) -> int:
    """Drop SUPERSEDED changelog entries. Returns how many went.

    Neither changelog had any retention at all (round 2 finding N6): one row
    per synced-table write, forever, on a library the repo measures at 8355
    tracks with many analyzed fields each.

    What makes this safe is that it only ever deletes an entry that is NOT
    the newest for its ``(table_name, row_pk)``. Both changelogs are read by
    :func:`_changelog_rows`, which reads each row LIVE and collapses several
    entries for one row into one change, so a superseded entry carries no
    information the newest one does not. Coverage is therefore unchanged: a
    spoke pulling from seq 0 still gets every row that ever changed, and
    ``MAX(seq)`` cannot move, so the prune cannot look like a hub restore
    (:mod:`apps.sync_hub.generation`).

    ``keep_days`` and ``keep_rows`` are belt on top of that: an entry has to
    be superseded AND older than ``keep_days`` AND outside the newest
    ``keep_rows`` entries before it is eligible. Both are compared as the
    protocol compares -- ``received_at`` lexicographically, which is an
    ordering over instants because every writer emits the canonical UTC
    format (``apps.shared.state.sync_stamp.CANONICAL_FORMAT``).
    """
    if changelog not in CHANGELOG_TABLES:
        raise SyncApplyError(
            f"{changelog!r} is not a changelog table; expected one of "
            f"{sorted(CHANGELOG_TABLES)}."
        )
    if keep_days < 0:
        raise SyncApplyError(f"keep_days must be >= 0, got {keep_days}")
    if keep_rows < 0:
        raise SyncApplyError(f"keep_rows must be >= 0, got {keep_rows}")
    cutoff_at = (
        datetime.now(UTC) if now is None else sync_stamp.parse_canonical(now)
    ) - timedelta(days=keep_days)
    cursor = conn.execute(
        f"""
        DELETE FROM {changelog}
        WHERE seq NOT IN (
            SELECT MAX(seq) FROM {changelog} GROUP BY table_name, row_pk
        )
          AND received_at < ?
          AND seq <= (SELECT COALESCE(MAX(seq), 0) FROM {changelog}) - ?
        """,
        (sync_stamp.canonical_from(cutoff_at), int(keep_rows)),
    )
    return int(cursor.rowcount)


__all__ = [
    "CHANGELOG_TABLES",
    "DEFAULT_KEEP_DAYS",
    "DEFAULT_KEEP_ROWS",
    "DEFAULT_PULL_LIMIT",
    "ApplyResult",
    "ChangeBatch",
    "SyncApplyError",
    "SyncSchemaMismatch",
    "Watermark",
    "current_seq",
    "hub_apply",
    "hub_changes_since",
    "local_seq",
    "machines_snapshot",
    "merge_machines",
    "prune_changelog",
    "read_watermark",
    "spoke_apply",
    "spoke_push",
    "upsert_machine",
    "write_watermark",
]
