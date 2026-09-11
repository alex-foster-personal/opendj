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
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.shared.state import schema as state_schema
from apps.sync_hub import capabilities, client_refusal, engine, protocol, wire_version
from apps.sync_hub.transport import API_PREFIX, HubTransport, SyncTransportError

#: What this build advertises on every request that can be answered
#: partially (round 5 gate B-1). One comma-free token per request for the
#: GET side, because :meth:`HubTransport.get` carries flat string params and
#: FastAPI reads a single occurrence into a one-element list.
_ADVERTISED: tuple[str, ...] = capabilities.THIS_BUILD


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
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """One explicit transaction. A multi-table READ needs one too, so the
    digest describes a single snapshot rather than several (ADR 08 point 6b).
    """
    conn.execute("BEGIN")
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
    wire_fleet = [machine.to_wire() for machine in fleet]
    for chunk in _batched(rows, batch_rows):
        try:
            payload = channel.post(
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
        requests += 1
    return _PushOutcome(
        accepted=accepted,
        rejected=rejected,
        requests=requests,
        hub_quarantined=_total_reported(reported),
    )


def _pull_in_chunks(
    channel: HubTransport,
    conn: sqlite3.Connection,
    machine_id: str,
    since_seq: int,
    hub_machine_id: str,
    *,
    limit: int,
) -> _PullOutcome:
    """Drain the hub's changelog from ``since_seq``, applying as we go.

    Each chunk is applied in its own transaction: a failure part way through
    leaves the watermark unwritten, so the next sync re-pulls from the old
    floor and LWW rejects what already landed. Redundant, never lossy.
    """
    pulled = 0
    applied = 0
    requests = 0
    quarantined = 0
    reported: list[Any] = []
    cursor = int(since_seq)
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
            result = engine.spoke_apply(conn, incoming)
            applied += result.accepted
            quarantined += result.quarantined
        pulled += len(incoming)
        if not has_more:
            return _PullOutcome(
                pulled=pulled,
                applied=applied,
                requests=requests,
                seq=chunk_seq,
                hub_quarantined=_total_reported(reported),
                quarantined=quarantined,
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
    "_int_from",
    "_local_machine_row",
    "_machines_from",
    "_pull_in_chunks",
    "_push_in_batches",
    "_rows_from",
    "_total_reported",
    "_transaction",
    "state_db_path",
]
