"""Row-level diagnosis when post-sync digests diverge (CSSTATUS-09).

Compares sync-eligible canonical rows between a spoke and its hub so a
:class:`~apps.sync_hub.client.SyncDigestMismatch` names at least one
``(table, stable_id)`` pair and which side holds the newer stamp.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from apps.sync_hub import capabilities, protocol, sync_set
from apps.sync_hub.protocol_common import (
    MODIFIED_AT,
    SyncProtocolError,
    canonical_bytes,
    canonical_row,
    decode_row_pk,
    encode_row_pk,
    lww_key,
    table_columns,
)
from apps.sync_hub.transport import API_PREFIX, HubTransport, SyncTransportError

DEFAULT_SAMPLE_LIMIT: int = 10
MESSAGE_BUDGET: int = 2048
_SAMPLE_PREFIX: str = "sample: "


@dataclass(frozen=True)
class DigestDiffRow:
    """One row where local and hub canonical forms disagree."""

    table: str
    pk: tuple[str, ...]
    local_stamp: str | None
    hub_stamp: str | None
    newer_side: Literal["local", "hub", "equal", "missing_local", "missing_hub", "unknown"]
    reason: str

    @property
    def stable_id(self) -> str:
        return format_pk(self.table, self.pk)


@dataclass(frozen=True)
class HubRowSample:
    """One sync-eligible hub row in digest walk order."""

    pk: tuple[str, ...]
    canonical_hex: str
    updated_at: str | None
    origin_device_id: str | None
    modified_at: str | None = None


def format_pk(table: str, pk: Sequence[str]) -> str:
    return f"{table}/{'/'.join(pk)}"


def _stamp_label(table: str, values: Mapping[str, Any] | None) -> str | None:
    if values is None:
        return None
    stamp = lww_key(values, table=table)
    return f"{stamp[0]}@{stamp[1] or '-'}"


def _hub_stamp_values(hub_row: HubRowSample) -> dict[str, Any]:
    values: dict[str, Any] = {
        "updated_at": hub_row.updated_at,
        "origin_device_id": hub_row.origin_device_id,
    }
    if hub_row.modified_at is not None:
        values[MODIFIED_AT] = hub_row.modified_at
    return values


def _newer_side(
    table: str,
    local: Mapping[str, Any] | None,
    hub: Mapping[str, Any] | None,
) -> Literal["local", "hub", "equal", "missing_local", "missing_hub", "unknown"]:
    if local is None and hub is None:
        return "unknown"
    if local is None:
        return "missing_local"
    if hub is None:
        return "missing_hub"
    local_key, hub_key = lww_key(local, table=table), lww_key(hub, table=table)
    if local_key == hub_key:
        return "equal"
    return "local" if local_key > hub_key else "hub"


def _canonical_hex(table: str, columns: Sequence[str], row: Sequence[Any]) -> str:
    return hashlib.sha256(canonical_bytes(canonical_row(table, columns, row))).hexdigest()


def _pk_column_types(
    conn: sqlite3.Connection, table: str, pk_columns: Sequence[str]
) -> dict[str, str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    col_types = {str(row[1]): str(row[2]).upper() for row in rows}
    return {column: col_types[column] for column in pk_columns}


def _coerce_pk_bind(value: str, col_type: str) -> Any:
    if "INT" in col_type:
        return int(value)
    return value


def _pk_after_predicate(
    conn: sqlite3.Connection,
    table: str,
    spec: protocol.TableSpec,
    cursor_pk: tuple[str | None, ...],
) -> tuple[str, tuple[Any, ...]]:
    if len(cursor_pk) != len(spec.pk):
        raise SyncProtocolError(
            f"cursor pk length {len(cursor_pk)} does not match "
            f"{table} pk length {len(spec.pk)}"
        )
    pk_types = _pk_column_types(conn, table, spec.pk)
    pk_cols = ", ".join(spec.pk)
    placeholders = ", ".join("?" for _ in spec.pk)
    bound = tuple(
        _coerce_pk_bind(str(value), pk_types[column])
        for column, value in zip(spec.pk, cursor_pk, strict=True)
    )
    return f"({pk_cols}) > ({placeholders})", bound


def iter_eligible_local_rows(
    conn: sqlite3.Connection, table: str
) -> Iterator[tuple[tuple[str, ...], str, dict[str, Any]]]:
    """Yield ``(pk, canonical_hex, canonical_dict)`` for each sync-eligible row."""
    spec = sync_set.spec_for(table)
    held = sync_set.HeldKeys(conn)
    columns = table_columns(conn, table)
    order_by = ", ".join(spec.pk)
    pk_index = {column: index for index, column in enumerate(columns)}
    cursor = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order_by}"
    )
    for row in cursor:
        if sync_set.excluded_reason(conn, table, columns, row, spec, held) is not None:
            continue
        canonical = canonical_row(table, columns, row)
        pk = tuple(str(row[pk_index[column]]) for column in spec.pk)
        yield pk, _canonical_hex(table, columns, row), canonical


def hub_sync_row_page(
    conn: sqlite3.Connection,
    table: str,
    *,
    cursor: str | None,
    limit: int,
) -> tuple[list[HubRowSample], str | None]:
    """One page of sync-eligible hub rows in digest order."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    spec = sync_set.spec_for(table)
    held = sync_set.HeldKeys(conn)
    columns = table_columns(conn, table)
    order_by = ", ".join(spec.pk)
    pk_index = {column: index for index, column in enumerate(columns)}
    select_sql = f"SELECT {', '.join(columns)} FROM {table}"
    where_params: tuple[Any, ...] = ()
    if cursor is not None:
        cursor_pk = decode_row_pk(cursor)
        where_sql, where_params = _pk_after_predicate(conn, table, spec, cursor_pk)
        select_sql = f"{select_sql} WHERE {where_sql}"
    select_sql = f"{select_sql} ORDER BY {order_by}"

    rows: list[HubRowSample] = []
    next_cursor: str | None = None
    for db_row in conn.execute(select_sql, where_params):
        if sync_set.excluded_reason(conn, table, columns, db_row, spec, held) is not None:
            continue
        pk = tuple(str(db_row[pk_index[column]]) for column in spec.pk)
        canonical = canonical_row(table, columns, db_row)
        rows.append(
            HubRowSample(
                pk=pk,
                canonical_hex=_canonical_hex(table, columns, db_row),
                updated_at=canonical.get("updated_at"),
                origin_device_id=canonical.get("origin_device_id"),
                modified_at=canonical.get(MODIFIED_AT),
            )
        )
        if len(rows) >= limit:
            next_cursor = encode_row_pk(pk)
            break
    if len(rows) < limit:
        next_cursor = None
    return rows, next_cursor


def fetch_hub_rows(
    channel: HubTransport,
    machine_id: str,
    table: str,
) -> dict[tuple[str, ...], HubRowSample]:
    """Load every sync-eligible hub row for ``table`` via ``GET /rows``."""
    by_pk: dict[tuple[str, ...], HubRowSample] = {}
    cursor: str | None = None
    while True:
        params = {
            "machine_id": machine_id,
            "table": table,
            "limit": "500",
            "capabilities": capabilities.QUARANTINE_V1,
        }
        if cursor is not None:
            params["cursor"] = cursor
        payload = channel.get(f"{API_PREFIX}/rows", params)
        raw_rows = payload.get("rows")
        if not isinstance(raw_rows, list):
            raise SyncTransportError(f"rows response for {table} lacks a 'rows' array")
        for item in raw_rows:
            if not isinstance(item, dict):
                continue
            raw_pk = item.get("pk")
            canonical_hex = item.get("canonical_hex")
            if not isinstance(raw_pk, list) or not isinstance(canonical_hex, str):
                continue
            pk = tuple(str(value) for value in raw_pk)
            by_pk[pk] = HubRowSample(
                pk=pk,
                canonical_hex=canonical_hex,
                updated_at=item.get("updated_at"),
                origin_device_id=item.get("origin_device_id"),
                modified_at=item.get(MODIFIED_AT),
            )
        next_cursor = payload.get("next_cursor")
        if not next_cursor:
            break
        cursor = str(next_cursor)
    return by_pk


def divergent_rows(
    local_conn: sqlite3.Connection,
    hub_rows_by_pk: Mapping[tuple[str, ...], HubRowSample],
    tables: Sequence[str],
    *,
    limit: int = DEFAULT_SAMPLE_LIMIT,
) -> list[DigestDiffRow]:
    """Find the first ``limit`` rows whose canonical bytes differ."""
    found: list[DigestDiffRow] = []
    for table in tables:
        for pk, local_hex, local_canonical in iter_eligible_local_rows(local_conn, table):
            hub_row = hub_rows_by_pk.get(pk)
            if hub_row is None:
                found.append(
                    DigestDiffRow(
                        table=table,
                        pk=pk,
                        local_stamp=_stamp_label(table, local_canonical),
                        hub_stamp=None,
                        newer_side="missing_hub",
                        reason="row exists locally but not on hub",
                    )
                )
            elif hub_row.canonical_hex != local_hex:
                hub_canonical = _hub_stamp_values(hub_row)
                found.append(
                    DigestDiffRow(
                        table=table,
                        pk=pk,
                        local_stamp=_stamp_label(table, local_canonical),
                        hub_stamp=_stamp_label(table, hub_canonical),
                        newer_side=_newer_side(table, local_canonical, hub_canonical),
                        reason="canonical bytes differ",
                    )
                )
            if len(found) >= limit:
                return found
        for pk, hub_row in hub_rows_by_pk.items():
            if any(row.pk == pk and row.table == table for row in found):
                continue
            # hub-only rows are discovered when local walk missed them; only
            # check keys for this table by pk shape is awkward, so skip extra
            # hub-only scan unless we need more samples later.
            pass
    return found


def sample_divergence(
    channel: HubTransport,
    local_conn: sqlite3.Connection,
    machine_id: str,
    tables: Sequence[str],
    *,
    limit: int = DEFAULT_SAMPLE_LIMIT,
) -> list[DigestDiffRow]:
    """Fetch hub rows and return up to ``limit`` divergent samples."""
    hub_by_table: dict[str, dict[tuple[str, ...], HubRowSample]] = {}
    for table in tables:
        hub_by_table[table] = fetch_hub_rows(channel, machine_id, table)
    found: list[DigestDiffRow] = []
    for table in tables:
        hub_rows = hub_by_table[table]
        local_pks: set[tuple[str, ...]] = set()
        for pk, local_hex, local_canonical in iter_eligible_local_rows(local_conn, table):
            local_pks.add(pk)
            hub_row = hub_rows.get(pk)
            if hub_row is None:
                found.append(
                    DigestDiffRow(
                        table=table,
                        pk=pk,
                        local_stamp=_stamp_label(table, local_canonical),
                        hub_stamp=None,
                        newer_side="missing_hub",
                        reason="row exists locally but not on hub",
                    )
                )
            elif hub_row.canonical_hex != local_hex:
                hub_canonical = _hub_stamp_values(hub_row)
                found.append(
                    DigestDiffRow(
                        table=table,
                        pk=pk,
                        local_stamp=_stamp_label(table, local_canonical),
                        hub_stamp=_stamp_label(table, hub_canonical),
                        newer_side=_newer_side(table, local_canonical, hub_canonical),
                        reason="canonical bytes differ",
                    )
                )
            if len(found) >= limit:
                return found
        for pk, hub_row in hub_rows.items():
            if pk in local_pks:
                continue
            found.append(
                DigestDiffRow(
                    table=table,
                    pk=pk,
                    local_stamp=None,
                    hub_stamp=_stamp_label(table, _hub_stamp_values(hub_row)),
                    newer_side="missing_local",
                    reason="row exists on hub but not locally",
                )
            )
            if len(found) >= limit:
                return found
    return found


def format_diff_line(row: DigestDiffRow) -> str:
    newer = row.newer_side
    if newer in ("local", "hub"):
        stamp = row.local_stamp if newer == "local" else row.hub_stamp
        return f"{row.stable_id} {newer} newer (stamp {stamp})"
    return f"{row.stable_id} {row.reason}"


def format_mismatch_message(
    *,
    hub_machine_id: str,
    rounds: int,
    divergent: Sequence[str],
    local_overall: str,
    remote_overall: str,
    diffs: Sequence[DigestDiffRow],
    total_estimate: int | None = None,
) -> str:
    base = (
        f"SyncDigestMismatch: post-sync digest mismatch against hub "
        f"{hub_machine_id} after {rounds} round(s): tables {list(divergent)} "
        f"differ (local overall {local_overall}, hub {remote_overall}). "
        f"No repair attempted; ADR 04 c6."
    )
    if not diffs:
        return base
    sample = ", ".join(format_diff_line(row) for row in diffs)
    remainder = ""
    if total_estimate is not None and total_estimate > len(diffs):
        remainder = f"; {total_estimate - len(diffs)} more"
    detail = f"{_SAMPLE_PREFIX}{sample}{remainder}"
    if len(base) + 1 + len(detail) > MESSAGE_BUDGET:
        detail = detail[: max(0, MESSAGE_BUDGET - len(base) - 1)]
    return f"{base} {detail}"


def parse_digest_samples(message: str) -> list[dict[str, str]] | None:
    """Parse structured diff samples from a journaled mismatch message."""
    idx = message.find(_SAMPLE_PREFIX)
    if idx < 0:
        return None
    sample_text = message[idx + len(_SAMPLE_PREFIX) :]
    if "; " in sample_text and " more" in sample_text:
        sample_text = sample_text.rsplit("; ", 1)[0]
    rows: list[dict[str, str]] = []
    for part in sample_text.split(", "):
        part = part.strip()
        if not part:
            continue
        if " newer (stamp " in part:
            head, stamp = part.split(" newer (stamp ", 1)
            stamp = stamp.removesuffix(")")
            table, stable = head.split("/", 1)
            side = "local" if " local newer" in part else "hub"
            rows.append(
                {
                    "table": table,
                    "stable_id": stable,
                    "newer_side": side,
                    "stamp": stamp,
                }
            )
        elif "/" in part:
            table, stable = part.split("/", 1)
            rows.append({"table": table, "stable_id": stable, "newer_side": "unknown", "stamp": ""})
    return rows or None


def samples_to_wire(samples: Sequence[DigestDiffRow]) -> list[dict[str, str]]:
    return [
        {
            "table": row.table,
            "stable_id": "/".join(row.pk),
            "newer_side": row.newer_side,
            "local_stamp": row.local_stamp or "",
            "hub_stamp": row.hub_stamp or "",
            "reason": row.reason,
        }
        for row in samples
    ]


__all__ = [
    "DEFAULT_SAMPLE_LIMIT",
    "DigestDiffRow",
    "HubRowSample",
    "divergent_rows",
    "fetch_hub_rows",
    "format_diff_line",
    "format_mismatch_message",
    "format_pk",
    "hub_sync_row_page",
    "iter_eligible_local_rows",
    "parse_digest_samples",
    "sample_divergence",
    "samples_to_wire",
]
