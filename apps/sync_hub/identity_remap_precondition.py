"""Transactional remap claims and their optional push wire fields (CLOUDSYNC-07).

The hub checks against its current identity election under the apply write lock.
Contradictions are alarms, never instructions to change the spoke's remap table.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from apps.sync_hub import capabilities
from apps.sync_hub.engine_apply import ApplyResult
from apps.sync_hub.identity_verdicts import IdentityLoserVerdicts
from apps.sync_hub.protocol import RowChange, SyncProtocolError
from apps.sync_hub.rejected_rows import (
    MAX_REJECTED_ROWS_NAMED,
    REASON_IDENTITY_REMAP_CONTRADICTED,
    RejectedRow,
)
from apps.sync_hub.transport import SyncTransportError

#: Default JSON separators insert these bytes between rows.
_ITEM_SEPARATOR_BYTES: int = len(", ")

def _json_bytes(value: object) -> int:
    """Bytes ``value`` takes in a request body as :class:`HttpTransport` encodes it."""
    return len(json.dumps(value).encode("utf-8"))


def _push_batches(
    wire_rows: Sequence[dict[str, Any]],
    *,
    max_rows: int,
    max_bytes: int,
    envelope_bytes: int,
) -> Iterator[tuple[dict[str, Any], ...]]:
    """Split ``wire_rows`` into push bodies of at most ``max_rows`` rows and ``max_bytes``.

    ``envelope_bytes`` is the body with no rows: machine id, fleet,
    capabilities. A row whose body alone is over ``max_bytes`` still travels,
    alone, so a limit this client cannot see (the hub's or a proxy's) decides
    it; a 413 on it is then loud (:func:`_push_chunk_with_split`).

    Order matters: :func:`apps.sync_hub.engine.spoke_push` returns rows
    parents-first, and each chunk is applied in its own hub transaction, so
    a reordering here would present a child row before the row it references
    and the FK would refuse it.
    """
    if max_rows < 1:
        raise ValueError(f"batch size must be >= 1, got {max_rows}")
    chunk: list[dict[str, Any]] = []
    size = envelope_bytes
    for wire in wire_rows:
        row_bytes = _json_bytes(wire) + _ITEM_SEPARATOR_BYTES
        if chunk and (len(chunk) == max_rows or size + row_bytes > max_bytes):
            yield tuple(chunk)
            chunk = []
            size = envelope_bytes
        chunk.append(wire)
        size += row_bytes
    if chunk:
        yield tuple(chunk)


def pairs_to_wire(pairs: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"loser_pk": loser, "survivor_pk": survivor} for loser, survivor in pairs]


def pairs_from_wire(raw: object) -> list[tuple[str, str]]:
    """Decode an optional field, refusing malformed claims rather than ignoring them."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SyncProtocolError("identity remap pairs must be an array")
    pairs = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise SyncProtocolError("identity remap pair must be an object")
        loser, survivor = item.get("loser_pk"), item.get("survivor_pk")
        if not isinstance(loser, str) or not loser or not isinstance(survivor, str) or not survivor:
            raise SyncProtocolError("identity remap pair needs non-empty loser_pk and survivor_pk")
        pairs.append((loser, survivor))
    return pairs


def independently_live(conn: sqlite3.Connection, stable_id: str) -> bool:
    """A live stored track that the hub does not elect as an identity loser."""
    return _independently_live(conn, stable_id, IdentityLoserVerdicts(conn))


def _independently_live(
    conn: sqlite3.Connection, stable_id: str, verdicts: IdentityLoserVerdicts
) -> bool:
    live = conn.execute(
        "SELECT 1 FROM tracks WHERE stable_id = ? AND deleted_at IS NULL", (stable_id,)
    ).fetchone()
    return live is not None and not verdicts.is_loser(stable_id)


def contradicted_pairs(
    conn: sqlite3.Connection, pairs: Sequence[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Read once per walk; the caller holds the same write lock through apply."""
    verdicts = IdentityLoserVerdicts(conn)
    return sorted({pair for pair in pairs if _independently_live(conn, pair[0], verdicts)})


def split_offer(
    changes: Sequence[RowChange], contradicted: Sequence[tuple[str, str]]
) -> tuple[list[RowChange], list[RejectedRow]]:
    """Hold back children and whole membership bundles naming contradicted targets."""
    targets = {survivor for _, survivor in contradicted}
    kept, rejected = [], []
    for row in changes:
        named = {str(row.values.get("stable_id"))}
        named.update(str(member.get("stable_id")) for member in row.members or ())
        if row.table != "tracks" and targets & named:
            rejected.append(RejectedRow(row.table, row.pk, REASON_IDENTITY_REMAP_CONTRADICTED))
        else:
            kept.append(row)
    return kept, rejected


def merge_apply_result(result: ApplyResult, rejected: Sequence[RejectedRow]) -> ApplyResult:
    """Count every held row; keep the existing bounded rejected-row diagnostics."""
    return dataclasses.replace(
        result,
        rejected=result.rejected + len(rejected),
        rejected_rows=(tuple(rejected) + result.rejected_rows)[:MAX_REJECTED_ROWS_NAMED],
    )


def _push_body(
    machine_id: str,
    wire_rows: Sequence[dict[str, Any]],
    wire_fleet: list[dict[str, object]],
    *,
    schema_version: int,
    wire_version: int,
    reseed: bool = False,
    identity_remap_pairs: Sequence[tuple[str, str]] = (),
) -> dict[str, object]:
    body: dict[str, object] = {
        "machine_id": machine_id,
        "schema_version": schema_version,
        "wire_version": wire_version,
        "rows": list(wire_rows),
        "machines": wire_fleet,
        "capabilities": list(capabilities.THIS_BUILD),
        "reseed": reseed,
    }
    if identity_remap_pairs:
        body["identity_remap_pairs"] = pairs_to_wire(identity_remap_pairs)
    return body


def _merge_split_push_payloads(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    """Keep every typed reject when a 413/timeout splits a push recursively."""
    merged: dict[str, Any] = {"quarantined": left.get("quarantined")}
    for key in ("accepted", "rejected"):
        values = [left.get(key), right.get(key)]
        if any(not isinstance(value, int) for value in values):
            raise SyncTransportError(f"push response lacks an integer {key!r}")
        merged[key] = sum(values)
    for key in ("identity_rejects", "rejected_rows", "identity_remaps_contradicted"):
        merged[key] = list(left.get(key) or []) + list(right.get(key) or [])
    return merged
