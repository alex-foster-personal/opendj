"""Reading and serializing changes: what a spoke offers, what a pull returns.

Split out of :mod:`apps.sync_hub.engine` (quality-gate file_size ratchet,
round 4). Push selection (:func:`spoke_push`) and one pull chunk
(:func:`hub_changes_since`) share :func:`_changelog_rows` -- a changelog
records THAT a row changed, never what it held, so both directions read the
named rows live and collapse to their newest content. Kept together rather
than split further because splitting that shared helper away from either
caller would just move the coupling, not remove it.

The logger keeps the ``apps.sync_hub.engine`` name (not this module's
``__name__``) because ``tests/cloudsync/test_hub_sync_round3.py`` asserts on
it via ``caplog.at_level(..., logger="apps.sync_hub.engine")`` -- that
contract predates this split and moving the log call should not silently
change what it names.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from apps.shared.state.sync_stamp import LOCAL_CHANGELOG_TABLE
from apps.sync_hub import protocol
from apps.sync_hub.engine_common import (
    _APPLY_ORDER,
    HUB_CHANGELOG_TABLE,
    SyncApplyError,
    _pk_predicate,
)
from apps.sync_hub.engine_watermark import Watermark, local_seq
from apps.sync_hub.protocol import (
    MEMBERSHIP_TABLE,
    SPEC_BY_TABLE,
    SYNC_TABLES,
    RowChange,
    TableSpec,
)

log = logging.getLogger("apps.sync_hub.engine")


# ----- push --------------------------------------------------------------


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
      :func:`apps.sync_hub.engine_watermark.local_seq` read before selection,
      so a write that commits during the round trip lands above the fence
      rather than in a gap.

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


__all__ = [
    "ChangeBatch",
    "hub_changes_since",
    "spoke_push",
]
