"""Wire decoding, batching and the chunked push/pull HTTP calls.

Split out of :mod:`apps.sync_hub.client` (quality-gate file_size ratchet,
round 4): this module is "how bytes cross the wire in one direction",
:mod:`apps.sync_hub.client` itself is "the round trip and when to stop".
Needs :data:`apps.sync_hub.client.PUSH_BATCH_ROWS` and
:data:`apps.sync_hub.client.PULL_LIMIT` back from that module -- imported
only after those two are defined there, so there is no import cycle.
"""
from __future__ import annotations

import itertools
import logging
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.shared.state import schema as state_schema
from apps.sync_hub import capabilities, client_refusal, engine, protocol, wire_version
from apps.sync_hub.engine_identity_map import (
    IdentityRepairRequest,
    apply_hub_identity_rejects,
)
from apps.sync_hub.transport import (
    API_PREFIX,
    HubTransport,
    SyncTransportError,
    is_timeout_transport,
)

#: What this build advertises on every request that can be answered
#: partially (round 5 gate B-1). One comma-free token per request for the
#: GET side, because :meth:`HubTransport.get` carries flat string params and
#: FastAPI reads a single occurrence into a one-element list.
_ADVERTISED: tuple[str, ...] = capabilities.THIS_BUILD

log = logging.getLogger(__name__)


def _local_machine_row(
    conn: sqlite3.Connection, machine_id: str
) -> protocol.MachineRow:
    row = conn.execute(
        "SELECT machine_id, name, platform, is_hub, data_root, first_seen, last_seen "
        "FROM machines WHERE machine_id = ?",
        (machine_id,),
    ).fetchone()
    if row is None:
        raise SyncTransportError(
            f"machine {machine_id} was just registered but is not in machines; "
            f"the local DB is not writable."
        )
    return protocol.MachineRow(
        machine_id=str(row[0]),
        name=str(row[1]),
        platform=str(row[2]),
        is_hub=bool(row[3]),
        data_root=None if row[4] is None else str(row[4]),
        first_seen=str(row[5]),
        last_seen=str(row[6]),
    )


def _machines_from(payload: Mapping[str, Any], label: str) -> list[protocol.MachineRow]:
    raw = payload.get("machines")
    if not isinstance(raw, list):
        raise SyncTransportError(f"{label} response lacks a 'machines' array")
    return [protocol.MachineRow.from_wire(item) for item in raw]


def _rows_from(payload: Mapping[str, Any], label: str) -> list[protocol.RowChange]:
    raw = payload.get("rows")
    if not isinstance(raw, list):
        raise SyncTransportError(f"{label} response lacks a 'rows' array")
    return [protocol.RowChange.from_wire(item) for item in raw]


def _int_from(payload: Mapping[str, Any], key: str, label: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise SyncTransportError(f"{label} response lacks an integer {key!r}")
    return value


def state_db_path(data_dir: Path) -> Path:
    """``<data-dir>/state/state.db`` -- the layout D1 of the spec fixes."""
    return Path(data_dir) / "state" / "state.db"


def _batched(
    rows: Sequence[protocol.RowChange], size: int
) -> Iterator[Sequence[protocol.RowChange]]:
    """Split ``rows`` into ``size``-row chunks, preserving order.

    Order matters: :func:`apps.sync_hub.engine.spoke_push` returns rows
    parents-first, and each chunk is applied in its own hub transaction, so
    a reordering here would present a child row before the row it references
    and the FK would refuse it.
    """
    if size < 1:
        raise ValueError(f"batch size must be >= 1, got {size}")
    iterator = iter(rows)
    while True:
        chunk = tuple(itertools.islice(iterator, size))
        if not chunk:
            return
        yield chunk


@contextmanager
def _transaction(conn: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """One explicit transaction. A multi-table READ needs one too, so the
    digest describes a single snapshot rather than several (ADR 08 point 6b).
    ``immediate`` takes the write lock up front, for a decision that must not
    see a local write land between reading the state and changing it.
    """
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _total_reported(reports: Sequence[Any]) -> int | None:
    """Sum peer-reported counts, or ``None`` when the peer did not report.

    ``None`` covers both "an older build omitted the field" and "no request
    of this kind was made, so the peer was never asked". Neither is zero: a
    peer that was not asked has not answered, and reading an unmeasured
    subject as a clean result is the defect ``.claude/rules/verification.md``
    exists to stop.
    """
    if not reports:
        return None
    if any(not isinstance(value, int) or isinstance(value, bool) for value in reports):
        return None
    return sum(int(value) for value in reports)


@dataclass(frozen=True)
class _PushOutcome:
    accepted: int
    rejected: int
    requests: int
    #: Rows of OURS the hub refused because ITS local copy carries a stamp it
    #: cannot order. ``None`` means the hub did not report.
    hub_quarantined: int | None = None
    #: Identity-collapse rejections from the hub (issue #3057).
    identity_rejects: tuple[protocol.IdentityReject, ...] = ()
    #: True when the hub answered a push request with 403
    #: ``entitlement_not_in_plan`` (:mod:`apps.sync_hub.client_refusal`).
    #: The batches before it are counted; nothing after it was sent.
    refused: bool = False


@dataclass(frozen=True)
class _PullOutcome:
    pulled: int
    applied: int
    requests: int
    seq: int
    #: Rows the hub could not OFFER because its own stored stamp is
    #: unorderable. ``None`` means the hub did not report.
    hub_quarantined: int | None = None
    #: Incoming rows THIS machine refused because the LOCAL row they meet
    #: cannot be ordered. Always measured, so never None.
    quarantined: int = 0
    identity_repairs: tuple[IdentityRepairRequest, ...] = ()


def _post_push_chunk(
    channel: HubTransport,
    machine_id: str,
    chunk: Sequence[protocol.RowChange],
    wire_fleet: list[dict[str, object]],
) -> dict[str, object]:
    return channel.post(
        f"{API_PREFIX}/push",
        {
            "machine_id": machine_id,
            "schema_version": state_schema.SCHEMA_VERSION,
            "wire_version": wire_version.WIRE_VERSION,
            "rows": [change.to_wire() for change in chunk],
            "machines": wire_fleet,
            "capabilities": list(_ADVERTISED),
        },
    )


def _push_chunk_with_split(
    channel: HubTransport,
    machine_id: str,
    chunk: Sequence[protocol.RowChange],
    wire_fleet: list[dict[str, object]],
) -> tuple[dict[str, object], int]:
    """Push one chunk, splitting once on client timeout."""
    try:
        return _post_push_chunk(channel, machine_id, chunk, wire_fleet), 1
    except SyncTransportError as exc:
        if not is_timeout_transport(exc) or len(chunk) <= 1:
            raise
        mid = len(chunk) // 2
        log.warning(
            "push timed out on %d row(s); retrying as %d then %d",
            len(chunk),
            mid,
            len(chunk) - mid,
        )
        left, left_requests = _push_chunk_with_split(
            channel, machine_id, chunk[:mid], wire_fleet
        )
        right, right_requests = _push_chunk_with_split(
            channel, machine_id, chunk[mid:], wire_fleet
        )
        return {
            "accepted": _int_from(left, "accepted", "push")
            + _int_from(right, "accepted", "push"),
            "rejected": _int_from(left, "rejected", "push")
            + _int_from(right, "rejected", "push"),
            "quarantined": left.get("quarantined"),
            "identity_rejects": list(left.get("identity_rejects") or [])
            + list(right.get("identity_rejects") or []),
        }, left_requests + right_requests


def _push_in_batches(
    channel: HubTransport,
    machine_id: str,
    rows: Sequence[protocol.RowChange],
    fleet: Sequence[protocol.MachineRow],
    *,
    batch_rows: int,
) -> _PushOutcome:
    """Offer ``rows`` to the hub, ``batch_rows`` at a time.

    Every chunk carries ``fleet`` -- this machine's whole ``machines``
    snapshot -- because the rows reference it: ``track_locations``,
    ``sync_policies`` and ``playlist_pins`` all point at
    ``machines(machine_id)``, and a spoke holds its peers' rows by design.
    A hub that has not met one of those machines answered the whole push
    with a FOREIGN KEY 409 (round 2 finding N4, round 1 A4). Each chunk
    carries it rather than only the first, so a chunk applied after a hub
    restart still lands.
    """
    accepted = 0
    rejected = 0
    requests = 0
    reported: list[Any] = []
    identity_rejects: list[protocol.IdentityReject] = []
    wire_fleet = [machine.to_wire() for machine in fleet]
    for chunk in _batched(rows, batch_rows):
        try:
            payload, chunk_requests = _push_chunk_with_split(
                channel, machine_id, chunk, wire_fleet
            )
        except SyncTransportError as exc:
            if not client_refusal.is_plan_refusal(exc):
                raise
            client_refusal.log_push_refused(exc, unsent=len(rows) - accepted - rejected)
            return _PushOutcome(
                accepted=accepted,
                rejected=rejected,
                requests=requests + 1,
                hub_quarantined=_total_reported(reported),
                refused=True,
            )
        accepted += _int_from(payload, "accepted", "push")
        rejected += _int_from(payload, "rejected", "push")
        reported.append(payload.get("quarantined"))
        identity_rejects.extend(_identity_rejects_from(payload))
        requests += chunk_requests
    return _PushOutcome(
        accepted=accepted,
        rejected=rejected,
        requests=requests,
        hub_quarantined=_total_reported(reported),
        identity_rejects=tuple(identity_rejects),
    )


def _identity_rejects_from(payload: Mapping[str, object]) -> list[protocol.IdentityReject]:
    raw = payload.get("identity_rejects")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise protocol.SyncProtocolError(
            f"push response 'identity_rejects' must be an array or absent, got {raw!r}"
        )
    return [protocol.IdentityReject.from_wire(item) for item in raw]


def _dedupe_repairs(
    repairs: Sequence[IdentityRepairRequest],
) -> tuple[IdentityRepairRequest, ...]:
    seen: set[tuple[str, str]] = set()
    ordered: list[IdentityRepairRequest] = []
    for repair in repairs:
        key = (repair.hub_survivor_pk, repair.offer_pk)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(repair)
    return tuple(ordered)


def _pull_repair_bundles(
    channel: HubTransport,
    conn: sqlite3.Connection,
    machine_id: str,
    hub_machine_id: str,
    stable_ids: Sequence[str],
    *,
    in_transaction: bool = False,
) -> tuple[int, tuple[IdentityRepairRequest, ...]]:
    """Fetch hub survivor bundles and apply them hub-authoritatively."""
    if not stable_ids:
        return 0, ()
    payload = channel.get(
        f"{API_PREFIX}/pull",
        {
            "machine_id": machine_id,
            "bundle_stable_ids": list(stable_ids),
            "capabilities": capabilities.QUARANTINE_V1,
        },
    )
    incoming = _rows_from(payload, "pull")

    def _apply() -> engine.ApplyResult:
        engine.merge_machines(
            conn, _machines_from(payload, "pull"), caller_id=hub_machine_id
        )
        return engine.spoke_apply(conn, incoming, repair_bundle=True)

    if in_transaction:
        result = _apply()
    else:
        with _transaction(conn):
            result = _apply()
    return len(incoming), result.identity_repairs


def _run_identity_repair(
    channel: HubTransport,
    conn: sqlite3.Connection,
    machine_id: str,
    hub_machine_id: str,
    fleet: Sequence[protocol.MachineRow],
    repairs: Sequence[IdentityRepairRequest],
    *,
    in_transaction: bool = False,
) -> tuple[int, int]:
    """Bounded one-round identity repair within the current sync transaction."""
    deduped = _dedupe_repairs(repairs)
    if not deduped:
        return 0, 0
    survivors = tuple({repair.hub_survivor_pk for repair in deduped})
    _pull_repair_bundles(
        channel,
        conn,
        machine_id,
        hub_machine_id,
        survivors,
        in_transaction=in_transaction,
    )
    offer_rows: list[protocol.RowChange] = []
    for repair in deduped:
        offer_rows.extend(engine.identity_repair_offer(conn, repair.offer_pk))
    if not offer_rows:
        return 0, 0
    push = _push_in_batches(
        channel,
        machine_id,
        offer_rows,
        fleet,
        batch_rows=1,
    )

    def _apply_rejects() -> None:
        if push.identity_rejects:
            apply_hub_identity_rejects(conn, push.identity_rejects)
        engine.finalize_identity_repairs(
            conn, tuple(repair.offer_pk for repair in deduped)
        )

    if in_transaction:
        _apply_rejects()
    else:
        with _transaction(conn):
            _apply_rejects()
    return len(offer_rows), push.accepted + push.rejected


def _pull_in_chunks(
    channel: HubTransport,
    conn: sqlite3.Connection,
    machine_id: str,
    since_seq: int,
    hub_machine_id: str,
    *,
    limit: int,
) -> _PullOutcome:
    """Drain the hub's changelog from ``since_seq``, applying once at the end.

    A pull window is a seq slice. Child rows in an early window can name
    parents logged later (track_locations, track_fields, track_vendor_ids,
    lyric_verdict, and playlist members). Applying per-chunk dies on FK.
    Buffer every RowChange until has_more is false, then one spoke_apply so
    apply_rank can order parents first across the whole snapshot. Machines
    still merge per-chunk: track_locations.machine_id is an FK. A failure
    leaves the watermark unwritten, so the next sync re-pulls from the old
    floor and LWW rejects what already landed.
    """
    pulled = 0
    applied = 0
    requests = 0
    quarantined = 0
    reported: list[Any] = []
    identity_repairs: list[IdentityRepairRequest] = []
    cursor = int(since_seq)
    pending: list[protocol.RowChange] = []
    while True:
        payload = channel.get(
            f"{API_PREFIX}/pull",
            {
                "machine_id": machine_id,
                "since_seq": str(cursor),
                "limit": str(limit),
                "capabilities": capabilities.QUARANTINE_V1,
            },
        )
        incoming = _rows_from(payload, "pull")
        pending.extend(incoming)
        chunk_seq = _int_from(payload, "seq", "pull")
        has_more = bool(payload.get("has_more", False))
        reported.append(payload.get("quarantined"))
        requests += 1
        with _transaction(conn):
            # The hub authored this snapshot, so it may refresh its OWN row and
            # only teach this spoke about peers it has not met; a poisoned peer
            # row cannot ride the pull back onto this machine (round 3 R1).
            engine.merge_machines(
                conn, _machines_from(payload, "pull"), caller_id=hub_machine_id
            )
        pulled += len(incoming)
        if not has_more:
            if pending:
                with _transaction(conn):
                    result = engine.spoke_apply(conn, pending)
                    applied += result.accepted
                    quarantined += result.quarantined
                    identity_repairs.extend(result.identity_repairs)
            return _PullOutcome(
                pulled=pulled,
                applied=applied,
                requests=requests,
                seq=chunk_seq,
                hub_quarantined=_total_reported(reported),
                quarantined=quarantined,
                identity_repairs=_dedupe_repairs(identity_repairs),
            )
        if chunk_seq <= cursor:
            raise SyncTransportError(
                f"hub {hub_machine_id} says more rows follow seq {chunk_seq} "
                f"but did not advance past {cursor}; refusing to loop forever "
                f"on a hub that cannot paginate."
            )
        cursor = chunk_seq


__all__ = [
    "_PullOutcome",
    "_PushOutcome",
    "_batched",
    "_dedupe_repairs",
    "_int_from",
    "_local_machine_row",
    "_machines_from",
    "_pull_in_chunks",
    "_push_in_batches",
    "_rows_from",
    "_run_identity_repair",
    "_total_reported",
    "_transaction",
    "state_db_path",
]
