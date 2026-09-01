"""Last-writer-wins apply: conflict resolution, upsert, membership replace.

Split out of :mod:`apps.sync_hub.engine` (quality-gate file_size ratchet,
round 4). ``hub_apply``/``spoke_apply`` are the two callers; everything else
here is private to how one batch of offered rows gets merged.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from apps.shared.state import sync_stamp
from apps.shared.state.sync_stamp import LOCAL_CHANGELOG_TABLE
from apps.sync_hub import protocol
from apps.sync_hub.engine_common import (
    _APPLY_ORDER,
    HUB_CHANGELOG_TABLE,
    SyncApplyError,
    SyncSchemaMismatch,
    _pk_predicate,
)
from apps.sync_hub.engine_watermark import current_seq
from apps.sync_hub.protocol import MEMBERSHIP_TABLE, SPEC_BY_TABLE, RowChange, TableSpec

# ----- apply -----------------------------------------------------------------


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of applying a batch of offered rows."""

    accepted: int
    rejected: int
    seq: int


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


__all__ = [
    "ApplyResult",
    "hub_apply",
    "spoke_apply",
]
