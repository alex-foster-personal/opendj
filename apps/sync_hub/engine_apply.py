"""Last-writer-wins apply: conflict resolution, upsert, membership replace.

Split out of :mod:`apps.sync_hub.engine` (quality-gate file_size ratchet,
round 4). ``hub_apply``/``spoke_apply`` are the two callers; everything else
here is private to how one batch of offered rows gets merged.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from apps.shared.state import sync_stamp
from apps.shared.state.sync_stamp import LOCAL_CHANGELOG_TABLE
from apps.sync_hub import protocol
from apps.sync_hub.engine_common import (
    HUB_CHANGELOG_TABLE,
    SyncApplyError,
    SyncSchemaMismatch,
    _pk_predicate,
    apply_rank,
)
from apps.sync_hub.engine_identity import (
    IdentityDecision,
    log_hash_conflict,
    names_held_parent,
    remap_track_children,
    resolve_track_identity,
    rewrite_incoming_change,
)
from apps.sync_hub.engine_identity_map import (
    IdentityRepairRequest,
    _remove_remap_loser,
    load_identity_remap,
    record_identity_remap,
)
from apps.sync_hub.engine_watermark import current_seq
from apps.sync_hub.protocol import MEMBERSHIP_TABLE, SPEC_BY_TABLE, RowChange, TableSpec

#: Same logger name as :mod:`apps.sync_hub.engine_changes`, for the reason
#: given there: ``caplog.at_level(..., logger="apps.sync_hub.engine")`` is an
#: existing test contract that predates the module split.
log = logging.getLogger("apps.sync_hub.engine")

#: Columns the merge orders a stored row by. Read together so one SELECT
#: serves both the sort key and the fault check.
_STAMP_COLUMNS: tuple[str, str] = (protocol.UPDATED_AT, protocol.ORIGIN_DEVICE_ID)


def _stamp_columns_for(table: str) -> tuple[str, ...]:
    if table == protocol.TRACK_FIELDS_TABLE:
        return _STAMP_COLUMNS + (protocol.MODIFIED_AT,)
    return _STAMP_COLUMNS


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ----- apply -----------------------------------------------------------------


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of applying a batch of offered rows.

    ``quarantined`` counts INCOMING rows refused because the LOCAL row they
    would be compared against carries a stamp this machine cannot order
    (round 5). Distinct from ``rejected``, which means the incoming row lost
    a comparison that actually happened: a quarantined row was never
    compared at all, and the local row it met is untouched.

    ``faults`` names the stored values behind that count (round 5 gate B-1).
    A caller that has to REFUSE the batch rather than report the shortfall --
    :mod:`apps.sync_hub.service` answering a peer too old to understand a
    partial answer -- needs the same detail ``origin/main``'s 422 carried, or
    the operator is handed a number and told to go looking.
    """

    accepted: int
    rejected: int
    seq: int
    quarantined: int = 0
    hash_pending: int = 0
    faults: tuple[protocol.StampFault, ...] = ()
    identity_conflicts: int = 0
    identity_rejects: tuple[protocol.IdentityReject, ...] = ()
    identity_repairs: tuple[IdentityRepairRequest, ...] = ()


@dataclass(frozen=True)
class _ApplyOneOutcome:
    """Result of applying one offered row."""

    status: str
    faults: tuple[protocol.StampFault, ...] = ()
    identity_reject: protocol.IdentityReject | None = None
    identity_repairs: tuple[IdentityRepairRequest, ...] = ()


@dataclass(frozen=True)
class _Resolution:
    """What the merge decided about one incoming row, before it is written.

    Exactly one of the three states holds. ``faults`` non-empty is the
    quarantine: the comparison could not be made, so nothing is written and
    the local row is left byte-identical.
    """

    loses: bool
    faults: tuple[protocol.StampFault, ...] = ()
    drop_stored_pks: tuple[str, ...] = ()
    rewrite_incoming_to: str | None = None
    identity_conflict: bool = False


def _stored_stamps(
    conn: sqlite3.Connection, spec: TableSpec, pk: Sequence[str]
) -> tuple[Any, ...] | None:
    """Stamp columns stored at ``pk``, or None.

    One read serving both :func:`_sort_key_of` and :func:`_faults_of`, so
    quarantining costs no extra query on the apply path.
    """
    columns = _stamp_columns_for(spec.name)
    return conn.execute(
        f"SELECT {', '.join(columns)} FROM {spec.name} WHERE {_pk_predicate(spec)}",
        tuple(pk),
    ).fetchone()


def _sort_key_of(table: str, stored: Sequence[Any]) -> tuple[str, str]:
    """Pure key. Only valid once :func:`_faults_of` came back empty."""
    columns = _stamp_columns_for(table)
    values = {column: stored[index] for index, column in enumerate(columns)}
    return protocol.lww_key(values, table=table)


def _faults_of(table: str, stored: Sequence[Any]) -> tuple[protocol.StampFault, ...]:
    return protocol.stored_stamp_faults(table, _stamp_columns_for(table), stored)


def _membership_faults(
    conn: sqlite3.Connection, playlist_id: str
) -> tuple[protocol.StampFault, ...]:
    """Stamp faults in the STORED bundle :func:`_replace_members` would delete.

    The OUTBOUND side already holds a whole playlist whose bundle carries an
    unorderable stamp (:func:`apps.sync_hub.sync_set.membership_reason`),
    because membership is whole-playlist (ADR 04 c5): dropping one member is
    not a quarantine, it is a silent edit telling the peer that track was
    REMOVED. The INBOUND side read only the ``playlists`` row, so a newer
    incoming playlist was accepted and :func:`_replace_members` DELETED the
    whole local bundle before reinserting the peer's -- destroying the exact
    rows the outbound quarantine refused to guess about, with no comparison
    ever made and nothing logged. That is the one loss
    :func:`_duplicate_stamps` and rule 4 of
    :mod:`apps.shared.state.sync_stamp` both exist to refuse.

    Reads :func:`apps.sync_hub.protocol.stored_stamp_faults` -- the same
    predicate as :func:`_faults_of`, the digest and the offer -- so the two
    directions cannot disagree about which bundle is orderable. Both stamp
    columns are checked, not ``updated_at`` alone: a tombstone's
    ``deleted_at`` decides whether the member is in the bundle at all
    (round 3 finding R8).
    """
    columns: tuple[str, str] = (protocol.UPDATED_AT, protocol.DELETED_AT)
    cursor = conn.execute(
        f"SELECT {', '.join(columns)} FROM {MEMBERSHIP_TABLE} "
        f"WHERE playlist_id = ?",
        (playlist_id,),
    )
    faults: list[protocol.StampFault] = []
    for row in cursor:
        faults.extend(
            protocol.stored_stamp_faults(MEMBERSHIP_TABLE, columns, row)
        )
    return tuple(faults)


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


def _duplicate_stamps(
    conn: sqlite3.Connection,
    spec: TableSpec,
    change: RowChange,
    conflict_pk: Sequence[str],
) -> tuple[Any, Any]:
    """Stored stamps of the local duplicate at ``conflict_pk``. Never None.

    A row that holds another row's natural key but carries no sort key was
    written by something that bypassed the sync columns entirely. The merge
    cannot order it against anything, and guessing is how a real edit gets
    overwritten by a row nothing can date.
    """
    stored = _stored_stamps(conn, spec, conflict_pk)
    if stored is None:
        raise SyncApplyError(
            f"{change.table}: row {list(conflict_pk)} holds the natural key of "
            f"{list(change.pk)} but has no sort key; the table was written by "
            f"something that bypassed the sync columns."
        )
    return stored


def _duplicate_incoming_wins(
    change: RowChange,
    stored: tuple[str, str],
    conflict_pk: tuple[str, ...],
    *,
    pull_defers_on_tie: bool = False,
) -> bool:
    """LWW between two rows that share a natural key under different pks.

    The tie is broken on the primary key itself rather than on arrival
    order: both peers evaluate the same rule over the same two rows, so both
    converge on the same survivor. Without it, two rows with identical
    ``(updated_at, origin_device_id)`` reject each other forever and the
    digest never matches (the shape of round 1 finding 5b).

    ``pull_defers_on_tie`` settles that TIE in the pulled row's favor and
    nothing else. A spoke re-running the pk tiebreak against a row the hub
    already elected is how the two disagree over which duplicate survives,
    so on a pull the hub's choice stands. It is a tiebreak override, not a
    stamp override: a duplicate that is STRICTLY newer still wins, because
    the stamps are comparable and the pulled row is simply the stale copy.
    """
    if change.sort_key != stored:
        return change.sort_key > stored
    if pull_defers_on_tie:
        # The hub already elected among tied rows; the spoke follows it.
        return True
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
            f"(missing={missing}, unexpected={extra}). Both machines must "
            f"speak the same apps.sync_hub.wire_version.WIRE_VERSION; if they "
            f"already do, a synced column changed without a wire bump."
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
    """Whole-playlist replace (ADR 04 c5). Runs only when the playlist won.

    The existence probe below is an FK-safety check, not a display filter:
    it exists only so a membership naming a track this machine has never
    heard of does not raise a FOREIGN KEY error, and it is deliberately NOT
    in ``test_soft_delete_read_guard.py``'s ``_ALLOWED_UNFILTERED_READS``
    filtering sense -- it is the sync layer, which "must see tombstones"
    per that guard's own carve-out (see the identical reasoning on
    :mod:`apps.sync_hub.engine_identity`'s membership read).

    A soft-deleted track's row still exists, so its membership is inserted
    like any other (round 5 trunk-red fix, Mon 14 Sep 2026): ADR 04 c5 makes
    the member list part of the winning playlist version's CONTENT, and the
    LWW oracle (``tests/cloudsync/sim_oracle.py``) never filters it by the
    member track's deleted_at either. A version that filtered here made the
    stored bundle depend on THIS machine's own delivery-order history of the
    track's tombstone rather than on the playlist's winning write, so two
    machines holding the identical winning (name, updated_at, origin) could
    still diverge on ``playlist_memberships`` -- exactly the persistent
    digest mismatch ``test_fleet_converges_to_the_lww_oracle`` caught
    (issue trunk-red-sim-property-digest). Only a track this machine has
    NEVER heard of is skipped, for FK safety; see
    ``tests/cloudsync/test_replace_members_soft_delete.py``.
    """
    columns = protocol.table_columns(conn, MEMBERSHIP_TABLE)
    conn.execute(f"DELETE FROM {MEMBERSHIP_TABLE} WHERE playlist_id = ?", (playlist_id,))
    sql = (
        f"INSERT INTO {MEMBERSHIP_TABLE} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})"
    )
    stored_tracks = _stored_track_ids(
        conn, [str(member.get("stable_id") or "") for member in members]
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
        track_id = str(member.get("stable_id") or "")
        if track_id not in stored_tracks:
            log.warning(
                "%s: playlist %s pos %r skipped; track %s is not here yet",
                MEMBERSHIP_TABLE, playlist_id, member.get("position"), track_id,
            )
            continue
        try:
            conn.execute(sql, tuple(member[column] for column in columns))
        except sqlite3.IntegrityError as exc:
            raise SyncApplyError(
                f"{MEMBERSHIP_TABLE}: playlist {playlist_id} position "
                f"{member.get('position')!r} violates a constraint ({exc})"
            ) from exc


def _stored_track_ids(conn: sqlite3.Connection, track_ids: Sequence[str]) -> frozenset[str]:
    """Which of ``track_ids`` a ``tracks`` row stores, in one read.

    A bundle checked each member with its own point read: 10,042 statements
    per replace of the 10,000-track fixture's playlist (LIBM-120 L6).
    """
    return frozenset(
        str(row[0])
        for row in conn.execute(
            "SELECT stable_id FROM tracks WHERE stable_id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(track_ids)),),
        )
    )


def _apply(
    conn: sqlite3.Connection,
    changes: Sequence[RowChange],
    *,
    record_changelog: bool,
    received_at: str | None = None,
    hub_authoritative: bool = False,
    hub_row_authority: bool = False,
) -> ApplyResult:
    # canonical_now(), not a local isoformat() call (round 3 finding R8): the
    # two disagree on a zero-microsecond tick, where isoformat() omits the
    # field and the bare +00:00 then sorts BELOW a canonical .000000+00:00
    # stamp naming the same instant -- hub_changelog.received_at would look a
    # fraction of a second older than it really is.
    stamp = received_at or sync_stamp.canonical_now()
    ordered = sorted(
        changes,
        key=lambda change: apply_rank(change.table, source="the offered batch"),
    )
    accepted = 0
    rejected = 0
    quarantined = 0
    hash_pending = 0
    identity_conflicts = 0
    faults: list[protocol.StampFault] = []
    remap: dict[str, str] = load_identity_remap(conn)
    held: set[str] = set()
    identity_rejects: list[protocol.IdentityReject] = []
    identity_repairs: list[IdentityRepairRequest] = []
    # One PRAGMA table_info per table for the batch, not one per row (LIBM-120 L6).
    with protocol.table_columns_memo(conn):
        for change in ordered:
            outcome = _apply_one(
                conn,
                change,
                remap,
                held,
                record_changelog,
                stamp,
                hub_authoritative=hub_authoritative,
                hub_row_authority=hub_row_authority,
            )
            if outcome.status == "accepted":
                accepted += 1
                if change.hash_pending:
                    hash_pending += 1
            elif outcome.status == "rejected":
                rejected += 1
                if outcome.identity_reject is not None:
                    identity_rejects.append(outcome.identity_reject)
            else:
                quarantined += 1
                faults.extend(outcome.faults)
                if outcome.status == "identity":
                    identity_conflicts += 1
            identity_repairs.extend(outcome.identity_repairs)
    return ApplyResult(
        accepted=accepted,
        rejected=rejected,
        seq=current_seq(conn),
        quarantined=quarantined,
        hash_pending=hash_pending,
        faults=tuple(faults),
        identity_conflicts=identity_conflicts,
        identity_rejects=tuple(identity_rejects),
        identity_repairs=tuple(identity_repairs),
    )


def _apply_one(
    conn: sqlite3.Connection,
    change: RowChange,
    remap: dict[str, str],
    held: set[str],
    record_changelog: bool,
    stamp: str,
    *,
    hub_authoritative: bool = False,
    hub_row_authority: bool = False,
) -> _ApplyOneOutcome:
    """Apply one rewritten row. Returns accepted/rejected/quarantined/identity."""
    if change.table == "tracks":
        rewritten = change
    else:
        rewritten = rewrite_incoming_change(change, remap)
    change = rewritten
    if names_held_parent(change, held):
        return _ApplyOneOutcome(status="identity")
    spec = SPEC_BY_TABLE[change.table]
    columns, values = _checked_values(conn, change.table, spec, change)
    verdict = _resolve_against_stored(
        conn,
        spec,
        change,
        hub_authoritative=hub_authoritative,
        hub_row_authority=hub_row_authority,
    )
    if verdict.identity_conflict:
        held.add(change.pk[0])
        _log_identity_conflict(change)
        return _ApplyOneOutcome(status="identity")
    if verdict.faults:
        _log_quarantine(change, verdict.faults)
        return _ApplyOneOutcome(status="quarantined", faults=verdict.faults)
    if verdict.loses:
        identity_reject: protocol.IdentityReject | None = None
        if verdict.rewrite_incoming_to is not None:
            record_identity_remap(
                conn, remap, change.pk[0], verdict.rewrite_incoming_to
            )
            identity_reject = protocol.IdentityReject(
                table="tracks",
                offered_pk=str(change.pk[0]),
                survivor_pk=verdict.rewrite_incoming_to,
            )
        return _ApplyOneOutcome(status="rejected", identity_reject=identity_reject)
    # Survivor PK must exist before children remap onto it. Incoming-wins
    # identity collapse writes the incoming row first, then moves stored
    # children, then drops the loser. The other order is a FOREIGN KEY
    # failure: the incoming PK is not stored yet.
    repairs: list[IdentityRepairRequest] = []
    _upsert(conn, change.table, spec, columns, values)
    for stored_pk in verdict.drop_stored_pks:
        if hub_authoritative:
            _remove_remap_loser(conn, remap, str(change.pk[0]))
            for loser, mapped in list(remap.items()):
                if loser == stored_pk:
                    continue
                if mapped == stored_pk:
                    record_identity_remap(
                        conn, remap, loser, str(change.pk[0])
                    )
        record_identity_remap(conn, remap, stored_pk, change.pk[0])
        remap_track_children(conn, stored_pk, change.pk[0])
        if hub_authoritative and change.table == "tracks":
            repairs.append(
                IdentityRepairRequest(
                    hub_survivor_pk=str(change.pk[0]),
                    offer_pk=str(stored_pk),
                )
            )
        else:
            _drop_superseded(conn, spec, (stored_pk,))
    if change.table == "playlists" and change.members is not None:
        _replace_members(conn, change.pk[0], change.members)
    if record_changelog:
        _log_hub_change(conn, change, stamp)
    return _ApplyOneOutcome(status="accepted", identity_repairs=tuple(repairs))


def _log_identity_conflict(change: RowChange) -> None:
    log.error(
        "refusing the incoming %s row %s: it shares a content identity with "
        "a stored row but their identity signals disagree (content_hash vs "
        "ISRC). Neither row is collapsed and the local row is untouched.",
        change.table,
        list(change.pk),
    )


def _log_quarantine(
    change: RowChange, faults: Sequence[protocol.StampFault]
) -> None:
    log.error(
        "refusing the incoming %s row %s: the LOCAL row it would be compared "
        "against cannot be ordered (%s), so the merge has no comparison to "
        "make. The local row is untouched and every other row in this batch "
        "still applies; repair it with `python -m apps.shared.state."
        "normalize_stamps --live`.",
        change.table,
        list(change.pk),
        protocol.describe_faults(faults),
    )


def _resolution_from_identity(
    conn: sqlite3.Connection,
    spec: TableSpec,
    decision: IdentityDecision,
    *,
    hub_authoritative: bool = False,
) -> _Resolution:
    """Translate a content-identity decision into an apply verdict.

    Stamp faults on a matched stored row still win: collapsing on a
    comparison that was never made is the loss round 5 refused.
    """
    if decision.kind == "conflict":
        return _Resolution(loses=False, identity_conflict=True)
    for pk in decision.stored_pks:
        stored = _stored_stamps(conn, spec, (pk,))
        if stored is None:
            continue
        faults = _faults_of(spec.name, stored)
        if faults:
            return _Resolution(loses=False, faults=faults)
    if hub_authoritative and decision.kind in ("incoming_wins", "incoming_loses"):
        return _Resolution(loses=False, drop_stored_pks=decision.stored_pks)
    if decision.kind == "incoming_loses":
        return _Resolution(loses=True, rewrite_incoming_to=decision.survivor_pk)
    return _Resolution(loses=False, drop_stored_pks=decision.stored_pks)


def _resolve_against_stored(
    conn: sqlite3.Connection,
    spec: TableSpec,
    change: RowChange,
    *,
    hub_authoritative: bool = False,
    hub_row_authority: bool = False,
) -> _Resolution:
    """Decide ``change`` against what is already stored, or quarantine it.

    Drops any duplicate it beats along the way as a side effect. Split out
    of :func:`_apply` to keep its own branch count under the quality-gate
    mccabe limit; the two paths are exactly what the loop body inlined
    before: no natural-key collision means a plain LWW compare against the
    primary key, a collision means beating EVERY duplicate it collides with.

    A LOCAL row whose stamp cannot be ordered quarantines the incoming row
    rather than raising (round 5): raising aborted the whole 200-row chunk,
    so one legacy row on the hub refused 199 innocent ones with it.

    For a ``playlists`` change carrying a bundle, the stored MEMBERSHIP rows
    are part of "what is stored" and are checked first: accepting the row is
    what licenses :func:`_replace_members` to delete them, so the bundle has
    to be judged before the playlist row wins, not after. See
    :func:`_membership_faults`.
    """
    if change.table == "playlists" and change.members is not None:
        member_faults = _membership_faults(conn, change.pk[0])
        if member_faults:
            return _Resolution(loses=False, faults=member_faults)
    if change.table == "tracks":
        decision = resolve_track_identity(conn, change)
        if decision.kind != "none":
            return _resolution_from_identity(
                conn, spec, decision, hub_authoritative=hub_authoritative
            )
    conflict_pks = _natural_conflict_pks(conn, spec, change)
    if not conflict_pks:
        stored = _stored_stamps(conn, spec, change.pk)
        if stored is None:
            return _Resolution(loses=False)
        faults = _faults_of(spec.name, stored)
        if faults:
            return _Resolution(loses=False, faults=faults)
        if change.table == "tracks":
            incoming_hash = _as_text(change.values.get("content_hash"))
            stored_row = conn.execute(
                "SELECT content_hash FROM tracks WHERE stable_id = ?",
                (change.pk[0],),
            ).fetchone()
            stored_hash = (
                _as_text(stored_row[0]) if stored_row is not None else None
            )
            if (
                incoming_hash
                and stored_hash
                and incoming_hash != stored_hash
            ):
                log_hash_conflict(
                    str(change.pk[0]),
                    incoming_hash,
                    stored_hash,
                    change.sort_key,
                    _sort_key_of(spec.name, stored),
                )
        if hub_row_authority:
            return _Resolution(loses=False)
        stored_key = _sort_key_of(spec.name, stored)
        if hub_authoritative:
            # A pull defers to the hub only on an exact tie: a tie cannot hide a
            # local edit (an edit stamps a newer key), but a strictly older
            # pulled row would overwrite one.
            return _Resolution(loses=change.sort_key < stored_key)
        return _Resolution(loses=change.sort_key <= stored_key)
    return _resolve_against_duplicates(
        conn,
        spec,
        change,
        conflict_pks,
        hub_authoritative=hub_authoritative,
        hub_row_authority=hub_row_authority,
    )


def _resolve_against_duplicates(
    conn: sqlite3.Connection,
    spec: TableSpec,
    change: RowChange,
    conflict_pks: Sequence[tuple[str, ...]],
    *,
    hub_authoritative: bool = False,
    hub_row_authority: bool = False,
) -> _Resolution:
    """LWW against every local row sharing this row's natural key.

    The incoming row has to beat EVERY duplicate it collides with; losing to
    one of them means the row it lost to is the survivor and this one is the
    stale copy. A duplicate whose own stamp cannot be ordered quarantines the
    incoming row: the alternative is hard-deleting that duplicate on a
    comparison that was never made, which is the exact loss
    :func:`_duplicate_stamps` refuses to guess at.

    ``hub_authoritative`` reaches the pk TIEBREAK in
    :func:`_duplicate_incoming_wins` and stops there. It does not license
    accepting the row outright: a pulled ``track_locations`` row can collide
    with one local row on the path index and a DIFFERENT local row on the
    url index (round 2 finding N5), and one that loses the url comparison is
    rejected WHOLE. Dropping every duplicate first, as the #3100 wiring did,
    hard-deleted the newer local row the pulled row had just lost to and left
    nothing in its place.
    """
    faults: list[protocol.StampFault] = []
    keys: list[tuple[tuple[str, ...], tuple[str, str]]] = []
    for pk in conflict_pks:
        stored = _duplicate_stamps(conn, spec, change, pk)
        row_faults = _faults_of(spec.name, stored)
        if row_faults:
            faults.extend(row_faults)
            continue
        keys.append((tuple(pk), _sort_key_of(spec.name, stored)))
    if faults:
        return _Resolution(loses=False, faults=tuple(faults))
    if hub_row_authority:
        for conflict_pk in conflict_pks:
            _drop_superseded(conn, spec, conflict_pk)
        return _Resolution(loses=False)
    if not all(
        _duplicate_incoming_wins(
            change, key, pk, pull_defers_on_tie=hub_authoritative
        )
        for pk, key in keys
    ):
        return _Resolution(loses=True)
    for conflict_pk in conflict_pks:
        _drop_superseded(conn, spec, conflict_pk)
    return _Resolution(loses=False)


def _log_hub_change(conn: sqlite3.Connection, change: RowChange, stamp: str) -> None:
    """One ``hub_changelog`` append for an accepted row. Split out of
    :func:`_apply` for the same reason as :func:`_resolve_against_stored`."""
    spec = SPEC_BY_TABLE[change.table]
    stored = _stored_stamps(conn, spec, change.pk)
    if stored is not None:
        columns = _stamp_columns_for(spec.name)
        values = {column: stored[index] for index, column in enumerate(columns)}
        updated_at, origin_device_id = protocol.lww_key(values, table=change.table)
    else:
        updated_at = change.updated_at
        origin_device_id = change.origin_device_id
    if not origin_device_id:
        origin_device_id = sync_stamp.ensure_local_machine(conn)
    conn.execute(
        """
        INSERT INTO hub_changelog(
            table_name, row_pk, updated_at, origin_device_id, received_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (change.table, change.row_pk, updated_at, origin_device_id, stamp),
    )


def finalize_identity_repairs(
    conn: sqlite3.Connection, offer_pks: Sequence[str]
) -> None:
    """Drop former survivor rows after a bounded identity repair offer."""
    if not offer_pks:
        return
    spec = SPEC_BY_TABLE["tracks"]
    for pk in offer_pks:
        _drop_superseded(conn, spec, (pk,))


def hub_apply(
    conn: sqlite3.Connection,
    changes: Sequence[RowChange],
    *,
    received_at: str | None = None,
) -> ApplyResult:
    """Merge a spoke's push into the hub DB and append to ``hub_changelog``."""
    return _apply(conn, changes, record_changelog=True, received_at=received_at)


def spoke_apply(
    conn: sqlite3.Connection,
    changes: Sequence[RowChange],
    *,
    repair_bundle: bool = False,
) -> ApplyResult:
    """Merge a hub pull into a spoke DB. Hub rows win identity collapse.

    Every other row still merges by row-level LWW (ADR-0004): an older pulled
    row must not overwrite a newer local edit, and a row that loses to one of
    two local duplicates must not drop the one it beat. On an exact tie the
    spoke defers to the hub's election, which cannot lose a local edit because
    an edit always stamps a newer key. Only the bounded
    identity repair bundle (``repair_bundle=True``, ADR-0074) is applied with
    the hub winning every row, because that bundle IS the hub's verdict for
    one identity pair and its children.

    A spoke keeps no changelog: its pull watermark is the hub's ``seq``, so a
    local changelog would only be a second, divergent numbering.
    """
    return _apply(
        conn,
        changes,
        record_changelog=False,
        hub_authoritative=True,
        hub_row_authority=repair_bundle,
    )


__all__ = [
    "ApplyResult",
    "finalize_identity_repairs",
    "hub_apply",
    "spoke_apply",
]
