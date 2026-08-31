"""Merge engine: push selection, last-writer-wins apply, changelog, watermarks.

Contract: ``specs/design_decision_04.md``. Nothing in this module stamps a
timestamp onto a domain row -- rows carry the ``updated_at`` /
``origin_device_id`` their originating machine wrote, and that pair is the
only thing the merge looks at. The single clock this module reads is
``hub_changelog.received_at``, which is hub bookkeeping and never syncs.

Two behaviours that look surprising until you know why:

* **Push selection is inclusive** (``updated_at >= watermark``, not ``>``).
  Two writes landing on the same ISO timestamp are unlikely in production and
  routine in tests with a frozen clock; an exclusive comparison would drop
  the second one forever. Re-offering the boundary rows costs one rejected
  row per sync, because :func:`hub_apply` accepts only a strictly greater
  ``(updated_at, origin_device_id)``. Redundant, never lossy.
* **A losing playlist row discards its membership bundle.** Membership is
  whole-playlist (ADR 04 c5): it replaces the peer's copy only when the
  ``playlists`` row itself wins, so a reorder and an add in the same window
  resolve to one of them, not to an interleaving of both.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from apps.sync_hub import protocol
from apps.sync_hub.protocol import (
    EPOCH,
    MEMBERSHIP_TABLE,
    SPEC_BY_TABLE,
    SYNC_TABLES,
    MachineRow,
    RowChange,
    TableSpec,
)


class SyncApplyError(RuntimeError):
    """A row could not be applied. Loud by design; sync stops here."""


class SyncSchemaMismatch(SyncApplyError):
    """A peer offered a row whose columns are not this DB's columns."""


_APPLY_ORDER: dict[str, int] = {
    spec.name: index for index, spec in enumerate(SYNC_TABLES)
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ----- watermarks (machine-local ``sync_state``) ---------------------------


@dataclass(frozen=True)
class Watermark:
    """This machine's position against one peer.

    ``last_sync_at`` is the push high-water mark: the greatest local
    ``updated_at`` this machine has already offered. ``last_pull_seq`` is the
    greatest ``hub_changelog.seq`` already applied.
    """

    peer: str
    last_push_seq: int = 0
    last_pull_seq: int = 0
    last_sync_at: str | None = None

    @property
    def push_floor(self) -> str:
        """The value push selection compares against."""
        return EPOCH if self.last_sync_at is None else self.last_sync_at


def read_watermark(conn: sqlite3.Connection, peer: str) -> Watermark:
    """Load the ``sync_state`` row for ``peer``; zeros if never synced."""
    row = conn.execute(
        "SELECT last_push_seq, last_pull_seq, last_sync_at "
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
    )


def write_watermark(conn: sqlite3.Connection, watermark: Watermark) -> None:
    """Upsert one ``sync_state`` row. Machine-local; never synced."""
    conn.execute(
        """
        INSERT INTO sync_state(peer, last_push_seq, last_pull_seq, last_sync_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(peer) DO UPDATE SET
            last_push_seq = excluded.last_push_seq,
            last_pull_seq = excluded.last_pull_seq,
            last_sync_at  = excluded.last_sync_at
        """,
        (
            watermark.peer,
            watermark.last_push_seq,
            watermark.last_pull_seq,
            watermark.last_sync_at,
        ),
    )


def local_high_water(conn: sqlite3.Connection) -> str:
    """Greatest ``updated_at`` across the local sync set (NULL reads as epoch).

    Used as the next push floor after a sync, so the following push offers
    only rows at or beyond the newest thing this machine has already shown
    the hub.
    """
    high = EPOCH
    for spec in SYNC_TABLES:
        row = conn.execute(
            f"SELECT MAX(COALESCE({protocol.UPDATED_AT}, ?)) FROM {spec.name}",
            (EPOCH,),
        ).fetchone()
        if row is not None and row[0] is not None and str(row[0]) > high:
            high = str(row[0])
    return high


# ----- hub sequence --------------------------------------------------------


def current_seq(conn: sqlite3.Connection) -> int:
    """Greatest ``hub_changelog.seq``; 0 on a machine that has never been a hub."""
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM hub_changelog").fetchone()
    return 0 if row is None else int(row[0])


# ----- machines registry ---------------------------------------------------


def upsert_machine(conn: sqlite3.Connection, machine: MachineRow) -> bool:
    """Insert or refresh one ``machines`` row. True if the DB changed.

    ``last_seen`` only moves forward, and ``first_seen`` is never rewritten,
    so this merge is monotone and needs no LWW columns -- which is fortunate,
    because the v6 DDL does not give ``machines`` any (see the note in
    :mod:`apps.sync_hub.protocol`).
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
        WHERE excluded.last_seen > machines.last_seen
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


def merge_machines(conn: sqlite3.Connection, machines: Iterable[MachineRow]) -> int:
    """Apply a registry snapshot from a peer. Returns rows changed."""
    changed = 0
    for machine in machines:
        try:
            if upsert_machine(conn, machine):
                changed += 1
        except sqlite3.IntegrityError as exc:
            raise SyncApplyError(
                f"machines row {machine.machine_id} ({machine.name!r}) conflicts "
                f"with a local row: {exc}. machines.name is UNIQUE, so two "
                f"machines sharing a hostname collide here rather than "
                f"silently overwriting each other."
            ) from exc
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


def _rows_for_table(
    conn: sqlite3.Connection, spec: TableSpec, *, floor: str
) -> list[RowChange]:
    columns = protocol.table_columns(conn, spec.name)
    order_by = ", ".join(spec.pk)
    cursor = conn.execute(
        f"SELECT {', '.join(columns)} FROM {spec.name} "
        f"WHERE COALESCE({protocol.UPDATED_AT}, ?) >= ? ORDER BY {order_by}",
        (EPOCH, floor),
    )
    changes: list[RowChange] = []
    for row in cursor:
        values = protocol.canonical_row(spec.name, columns, row)
        pk = tuple(str(values[column]) for column in spec.pk)
        members = (
            _members_for_playlist(conn, pk[0]) if spec.name == "playlists" else None
        )
        changes.append(
            RowChange(table=spec.name, pk=pk, values=values, members=members)
        )
    return changes


def spoke_push(
    conn: sqlite3.Connection, *, watermark: Watermark | None = None
) -> list[RowChange]:
    """Rows this machine offers the hub: everything at or beyond the floor.

    ``playlist_memberships`` never appears as a top-level change -- each
    ``playlists`` row carries its complete membership bundle instead
    (ADR 04 c5).
    """
    floor = EPOCH if watermark is None else watermark.push_floor
    changes: list[RowChange] = []
    for spec in SYNC_TABLES:
        changes.extend(_rows_for_table(conn, spec, floor=floor))
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
    stamp = received_at or _now_iso()
    ordered = sorted(changes, key=lambda change: _APPLY_ORDER[change.table])
    accepted = 0
    rejected = 0
    for change in ordered:
        spec = SPEC_BY_TABLE.get(change.table)
        if spec is None:
            raise SyncApplyError(f"{change.table!r} is not in the sync set")
        columns, values = _checked_values(conn, change.table, spec, change)
        stored = _stored_sort_key(conn, spec, change.pk)
        if stored is not None and change.sort_key <= stored:
            rejected += 1
            continue
        _upsert(conn, change.table, spec, columns, values)
        if change.table == "playlists" and change.members is not None:
            _replace_members(conn, change.pk[0], change.members)
        if record_changelog:
            conn.execute(
                """
                INSERT INTO hub_changelog(
                    table_name, row_pk, updated_at, origin_device_id, received_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    change.table,
                    change.row_pk,
                    change.updated_at,
                    change.origin_device_id,
                    stamp,
                ),
            )
        accepted += 1
    return ApplyResult(accepted=accepted, rejected=rejected, seq=current_seq(conn))


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
    """Rows a spoke should apply, plus the seq that consumed them."""

    rows: list[RowChange]
    seq: int


def hub_changes_since(conn: sqlite3.Connection, since_seq: int) -> ChangeBatch:
    """Every row touched after ``since_seq``, deduped to its current state.

    The changelog records that a row changed, not what it looked like at the
    time, so this reads each row live. Several entries for one row collapse
    to a single change carrying its newest content -- which is the same thing
    LWW would have converged on anyway, at a fraction of the payload.
    """
    entries = conn.execute(
        "SELECT seq, table_name, row_pk FROM hub_changelog "
        "WHERE seq > ? ORDER BY seq",
        (int(since_seq),),
    ).fetchall()
    latest: dict[tuple[str, str], int] = {}
    max_seq = int(since_seq)
    for seq, table_name, row_pk in entries:
        latest[(str(table_name), str(row_pk))] = int(seq)
        max_seq = max(max_seq, int(seq))

    rows: list[RowChange] = []
    for (table_name, row_pk), _seq in sorted(latest.items(), key=lambda item: item[1]):
        spec = SPEC_BY_TABLE.get(table_name)
        if spec is None:
            raise SyncApplyError(
                f"hub_changelog references table {table_name!r}, which is not "
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
            raise SyncApplyError(
                f"hub_changelog points at {table_name} row {row_pk} which no "
                f"longer exists. Deletes in the sync set are soft (ADR 04 c4); "
                f"something hard-deleted a synced row."
            )
        values = protocol.canonical_row(table_name, columns, row)
        members = (
            _members_for_playlist(conn, str(pk[0]))
            if table_name == "playlists"
            else None
        )
        rows.append(
            RowChange(
                table=table_name,
                pk=tuple(str(item) for item in pk),
                values=values,
                members=members,
            )
        )
    return ChangeBatch(rows=rows, seq=max_seq)


__all__ = [
    "ApplyResult",
    "ChangeBatch",
    "SyncApplyError",
    "SyncSchemaMismatch",
    "Watermark",
    "current_seq",
    "hub_apply",
    "hub_changes_since",
    "local_high_water",
    "machines_snapshot",
    "merge_machines",
    "read_watermark",
    "spoke_apply",
    "spoke_push",
    "upsert_machine",
    "write_watermark",
]
