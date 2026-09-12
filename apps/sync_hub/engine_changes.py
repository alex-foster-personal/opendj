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
from dataclasses import dataclass, replace
from typing import Any

from apps.shared.state import sync_stamp
from apps.shared.state.sync_stamp import LOCAL_CHANGELOG_TABLE
from apps.sync_hub import protocol, sync_set
from apps.sync_hub.engine_common import (
    apply_rank,
    HUB_CHANGELOG_TABLE,
    SyncApplyError,
    _pk_predicate,
)
from apps.sync_hub.engine_watermark import Watermark, local_seq
from apps.sync_hub.protocol import (
    MEMBERSHIP_SPEC,
    MEMBERSHIP_TABLE,
    SPEC_BY_TABLE,
    SYNC_TABLES,
    RowChange,
    TableSpec,
)

log = logging.getLogger("apps.sync_hub.engine")


# ----- push --------------------------------------------------------------


@dataclass(frozen=True)
class HeldRow:
    """One row a selection could NOT put on the wire, and where it came from.

    ``seq`` is the ``local_changelog`` entry that selected it, which only a
    FENCED walk has: a full offer walks the tables rather than the changelog,
    so there is no entry to hold a fence at and ``seq`` is None. That
    distinction is the whole of B1's second half -- see
    :func:`apps.sync_hub.engine_watermark.settled_push_seq` for the fenced
    answer and :func:`relog_held` for the full-offer one.
    """

    table: str
    pk: tuple[str, ...]
    seq: int | None = None


@dataclass(frozen=True)
class Offer:
    """The rows a spoke puts on the wire, and the rows it could not.

    ``held`` names every row excluded because a STORED stamp on this machine
    cannot be ordered, or because a row it references is itself held (round
    5). Identities rather than a bare count (round 5 gate B1): the count told
    an operator a row was held back but gave the fence nothing to hold ON, so
    the fence advanced past it and no later selection could name it again.

    ``quarantined`` is derived from ``held`` rather than tallied beside it,
    so the number an operator reads and the rows the fence protects cannot
    drift apart -- they are one list asked two questions.
    """

    rows: list[RowChange]
    held: tuple[HeldRow, ...] = ()

    @property
    def quarantined(self) -> int:
        """How many rows this machine held back."""
        return len(self.held)

    @property
    def held_seq(self) -> int | None:
        """Lowest changelog seq among the held rows; None when none has one.

        None means either nothing was held or a FULL offer held it, and the
        two are not the same state -- ``held`` distinguishes them, which is
        why the fence rule takes both.
        """
        seqs = [row.seq for row in self.held if row.seq is not None]
        return min(seqs) if seqs else None

    def __len__(self) -> int:
        return len(self.rows)


def _quarantine(table: str, pk: object, reason: str) -> None:
    """Log one row's exclusion, naming the cause. Never silent."""
    log.error(
        "%s row %s is NOT in the sync set: %s. It will not reach any peer "
        "until it is repaired with `python -m apps.shared.state."
        "normalize_stamps --live`; every other row still syncs.",
        table,
        pk,
        reason,
    )


def _members_for_playlist(
    conn: sqlite3.Connection, playlist_id: str, held: sync_set.HeldKeys
) -> tuple[dict[str, Any], ...] | None:
    """The whole membership bundle for ``playlist_id``, or None to hold the
    playlist back. See :func:`apps.sync_hub.sync_set.membership_reason`.
    """
    columns = protocol.table_columns(conn, MEMBERSHIP_TABLE)
    cursor = conn.execute(
        f"SELECT {', '.join(columns)} FROM {MEMBERSHIP_TABLE} "
        f"WHERE playlist_id = ? ORDER BY "
        f"COALESCE(order_key, printf('%08d', position)), position",
        (playlist_id,),
    )
    members: list[dict[str, Any]] = []
    for row in cursor:
        reason = sync_set.row_reason(
            MEMBERSHIP_TABLE, columns, row, MEMBERSHIP_SPEC, held
        )
        if reason is not None:
            _quarantine(MEMBERSHIP_TABLE, playlist_id, reason)
            return None
        members.append(protocol.canonical_row(MEMBERSHIP_TABLE, columns, row))
    return tuple(members)


def _row_change(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    spec: TableSpec,
    row: Sequence[Any],
    held: sync_set.HeldKeys,
) -> RowChange | HeldRow:
    """One offered row, or the :class:`HeldRow` this machine must hold back.

    Returns the held row's IDENTITY rather than None (round 5 gate B1): the
    caller has to be able to protect its fence against it, and a None cannot
    name what it stood for.
    """
    pk_index = {column: index for index, column in enumerate(columns)}
    raw_pk = tuple(str(row[pk_index[column]]) for column in spec.pk)
    reason = sync_set.row_reason(table, columns, row, spec, held)
    if reason is not None:
        _quarantine(
            table, [row[pk_index[column]] for column in spec.pk], reason
        )
        return HeldRow(table=table, pk=raw_pk)
    values = protocol.canonical_row(table, columns, row)
    pk = tuple(str(values[column]) for column in spec.pk)
    members = (
        _members_for_playlist(conn, pk[0], held) if table == "playlists" else None
    )
    if table == "playlists" and members is None:
        held.hold(table, pk)
        return HeldRow(table=table, pk=pk)
    return RowChange(table=table, pk=pk, values=values, members=members)


def _rows_for_table(
    conn: sqlite3.Connection, spec: TableSpec, held: sync_set.HeldKeys
) -> Offer:
    columns = protocol.table_columns(conn, spec.name)
    order_by = ", ".join(spec.pk)
    cursor = conn.execute(
        f"SELECT {', '.join(columns)} FROM {spec.name} ORDER BY {order_by}"
    )
    rows: list[RowChange] = []
    held_rows: list[HeldRow] = []
    for row in cursor:
        change = _row_change(conn, spec.name, columns, spec, row, held)
        if isinstance(change, HeldRow):
            held_rows.append(change)
            continue
        rows.append(change)
    return Offer(rows=rows, held=tuple(held_rows))


def _changelog_rows(
    conn: sqlite3.Connection,
    entries: Sequence[tuple[Any, Any, Any]],
    *,
    changelog: str,
) -> tuple[Offer, int]:
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

    Rows are returned in ``apply_rank`` order (parents before children), which
    the batching in :mod:`apps.sync_hub.client` depends on: a chunk boundary
    must never put a child row in an earlier request than its parent.

    Returns the offer (rows plus the count QUARANTINED for an unorderable
    stored stamp, round 5) and the number of entries SKIPPED because the row
    they name no longer exists (round 2 finding 4a; see the comment at the
    skip). The two counts are deliberately separate: "the row is gone" and
    "the row cannot be ordered" have different causes and different repairs.
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
    held_rows: list[HeldRow] = []
    held = sync_set.HeldKeys(conn)
    # Parents before children, so a held-back parent is already recorded when
    # its dependants are decided. The sort at the end orders what SURVIVES;
    # this one orders what is DECIDED, and the two are not the same pass.
    for table_name, row_pk in sorted(
        latest, key=lambda entry: (apply_rank(entry[0], source=changelog), entry[1])
    ):
        # apply_rank has already refused any name outside the sync set, and
        # both indexes are comprehensions over the same SYNC_TABLES, so this
        # lookup cannot miss. The invariant is pinned by
        # test_the_rank_index_and_the_spec_index_agree_on_the_sync_set.
        spec = SPEC_BY_TABLE[table_name]
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
        change = _row_change(conn, table_name, columns, spec, row, held)
        if isinstance(change, HeldRow):
            # The seq that SELECTED it, so the fence can stop below it. A
            # membership entry was resolved to its playlists row above, and
            # ``latest`` is keyed by the resolved pair, so this reads the
            # entry that actually put the row in this window.
            held_rows.append(replace(change, seq=latest[(table_name, row_pk)]))
            continue
        changes.append(change)
    changes.sort(
        key=lambda change: (apply_rank(change.table, source=changelog), change.pk)
    )
    return Offer(rows=changes, held=tuple(held_rows)), skipped


def spoke_push(
    conn: sqlite3.Connection,
    *,
    watermark: Watermark | None = None,
    ceiling: int | None = None,
) -> Offer:
    """The rows this machine offers its peer, and the rows it cannot.

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

    A row whose stored stamp cannot be ordered is QUARANTINED: left out of
    the offer, counted on :attr:`Offer.quarantined` and logged with its
    offending value. Round 4 raised instead, so one such row aborted the
    push before a single other row was offered, on every sync, forever.
    Quarantine is transitive over the FK graph (see :data:`_PARENT_KEYS`).
    """
    if watermark is None or watermark.needs_full_offer:
        changes: list[RowChange] = []
        held_rows: list[HeldRow] = []
        held = sync_set.HeldKeys(conn)
        for spec in SYNC_TABLES:
            offer = _rows_for_table(conn, spec, held)
            changes.extend(offer.rows)
            held_rows.extend(offer.held)
        return Offer(rows=changes, held=tuple(held_rows))
    top = local_seq(conn) if ceiling is None else int(ceiling)
    entries = conn.execute(
        f"SELECT seq, table_name, row_pk FROM {LOCAL_CHANGELOG_TABLE} "
        f"WHERE seq > ? AND seq <= ? ORDER BY seq",
        (watermark.last_push_seq, top),
    ).fetchall()
    offer, _skipped = _changelog_rows(conn, entries, changelog=LOCAL_CHANGELOG_TABLE)
    return offer


def relog_held(
    conn: sqlite3.Connection, held: Sequence[HeldRow], machine_id: str
) -> int:
    """Give every row a FULL offer held back a ``local_changelog`` entry.

    Returns how many entries were written. Runs in the caller's transaction.

    A fenced offer needs nothing like this: the window that selected a held
    row IS a changelog entry, so the fence stops one below it
    (:func:`apps.sync_hub.engine_watermark.settled_push_seq`) and the same
    entry re-selects the row on every later sync until it can travel. A FULL
    offer has no such entry -- it walks the tables, and on a library migrated
    from v5 the rows predate ``local_changelog`` entirely -- so after it the
    held rows are invisible to every fenced selection that follows. That is
    how a quarantined track's 62 cascade dependants get dropped one UI rename
    later: the rename frees the parent and logs the PARENT, while the
    dependants were HELD, not CHANGED, and no entry anywhere names them.

    Writing the entries here rather than re-logging on every sync is what
    keeps the changelog from growing without bound: once a held row has an
    entry above the fence, the fence rule alone keeps it selected, so this
    fires once per full offer rather than once per sync.

    ``stamp_and_log`` is the same choke point every writer uses, and the
    entry says exactly what is true -- this machine must offer this row
    again. Nothing orders ``local_changelog.updated_at`` (the fence reads
    ``seq, table_name, row_pk``), so a fresh canonical stamp here cannot
    reorder anything; the ROW keeps its unorderable stamp, because this is a
    re-queue and not a repair.
    """
    for row in held:
        sync_stamp.stamp_and_log(conn, row.table, row.pk, machine_id)
    return len(held)


# ----- pull ----------------------------------------------------------------


@dataclass(frozen=True)
class ChangeBatch:
    """Rows a spoke should apply, the seq that consumed them, and whether
    the hub still holds entries above that seq.

    ``skipped`` counts entries whose row is gone from the hub (round 2
    finding 4a). ``quarantined`` counts rows the hub holds but cannot order
    (round 5). Both are reported rather than inferred so an operator sees
    the numbers without reading the hub's log, and they are separate because
    they have different repairs.
    """

    rows: list[RowChange]
    seq: int
    has_more: bool = False
    skipped: int = 0
    quarantined: int = 0


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
    offer, skipped = _changelog_rows(conn, entries, changelog=HUB_CHANGELOG_TABLE)
    return ChangeBatch(
        rows=offer.rows,
        seq=max_seq,
        has_more=remaining is not None,
        skipped=skipped,
        quarantined=offer.quarantined,
    )


__all__ = [
    "ChangeBatch",
    "HeldRow",
    "Offer",
    "hub_changes_since",
    "relog_held",
    "spoke_push",
]
