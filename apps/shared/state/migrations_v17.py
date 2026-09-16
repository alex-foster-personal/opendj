"""Migration 16 -> 17: hub changelog stamp repair (#3171).

v16 (#3165) already created ``schema_meta_markers`` and the
``hub_changelog(table_name, row_pk)`` index. v17 adds the covering
``(table_name, row_pk, seq)`` index that the latest-row-per-key join needs,
and a one-shot hub-side repair that re-offers rows whose latest changelog
stamp disagrees with the live domain row.

The repair exists because a legacy hub-side backfill logged 23,416
``track_fields`` changelog rows (and 7,917 ``track_vendor_ids`` rows) carrying
the EPOCH comparison sentinel instead of the row's own ``updated_at``. The
sentinel loses every LWW comparison, so a spoke past those sequences keeps its
stale value permanently (#3057).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from apps.sync_hub.engine_common import _pk_predicate
from apps.sync_hub.protocol import SPEC_BY_TABLE, SYNC_TABLES, decode_row_pk
from apps.sync_hub.protocol_common import NO_ORIGIN

from . import machine_identity
from . import schema_markers
from . import sync_stamp

HUB_CHANGELOG_TABLE: str = "hub_changelog"
REPAIR_MARKER: str = "cloudsync_hub_changelog_stamp_repair_v1"

_V17: list[str] = [
    # ``schema_meta_markers`` and ``idx_hub_changelog_table`` arrive in v16
    # (#3165). This covering index is what turns the latest-row-per-key join
    # below into an index-only scan instead of the unindexed per-row probe
    # that locked the hub for nine minutes an open (#3165).
    "CREATE INDEX IF NOT EXISTS idx_hub_changelog_table_row_pk_seq "
    "ON hub_changelog(table_name, row_pk, seq)",
]


def _live_rows_union_sql() -> str:
    parts: list[str] = []
    for spec in SYNC_TABLES:
        pk_args = ", ".join(spec.pk)
        parts.append(
            f"SELECT '{spec.name}' AS table_name, json_array({pk_args}) AS row_pk, "
            f"updated_at, origin_device_id FROM {spec.name}"
        )
    return " UNION ALL ".join(parts)


def _candidate_query_sql() -> str:
    return f"""
    WITH latest AS (
        SELECT h.table_name, h.row_pk, h.updated_at AS changelog_updated_at
        FROM {HUB_CHANGELOG_TABLE} h
        INNER JOIN (
            SELECT table_name, row_pk, MAX(seq) AS max_seq
            FROM {HUB_CHANGELOG_TABLE}
            GROUP BY table_name, row_pk
        ) m ON h.table_name = m.table_name
           AND h.row_pk = m.row_pk
           AND h.seq = m.max_seq
    ),
    live AS (
        {_live_rows_union_sql()}
    )
    SELECT latest.table_name, latest.row_pk, live.updated_at, live.origin_device_id
    FROM latest
    INNER JOIN live
        ON latest.table_name = live.table_name
       AND latest.row_pk = live.row_pk
    WHERE latest.changelog_updated_at IS NOT live.updated_at
       OR (latest.changelog_updated_at IS NULL AND live.updated_at IS NOT NULL)
       OR (latest.changelog_updated_at IS NOT NULL AND live.updated_at IS NULL)
    """


def _scan_stamp_mismatch_candidates(
    conn: sqlite3.Connection,
) -> list[tuple[str, str, str | None, str | None]]:
    if not schema_markers.table_exists(conn, HUB_CHANGELOG_TABLE):
        return []
    return [
        (str(table_name), str(row_pk), updated_at, origin)
        for table_name, row_pk, updated_at, origin in conn.execute(
            _candidate_query_sql()
        ).fetchall()
    ]


def _latest_changelog_stamp(
    conn: sqlite3.Connection, table_name: str, row_pk: str
) -> str | None:
    row = conn.execute(
        f"SELECT updated_at FROM {HUB_CHANGELOG_TABLE} "
        "WHERE table_name = ? AND row_pk = ? ORDER BY seq DESC LIMIT 1",
        (table_name, row_pk),
    ).fetchone()
    return None if row is None else str(row[0])


def _fetch_live_stamp(
    conn: sqlite3.Connection,
    table_name: str,
    row_pk: str,
) -> tuple[str | None, str | None] | None:
    spec = SPEC_BY_TABLE.get(table_name)
    if spec is None:
        return None
    pk = tuple(str(part) for part in decode_row_pk(row_pk))
    row = conn.execute(
        f"SELECT updated_at, origin_device_id FROM {table_name} "
        f"WHERE {_pk_predicate(spec)}",
        pk,
    ).fetchone()
    if row is None:
        return None
    return row[0], row[1]


def _append_repair_changelog_rows(
    conn: sqlite3.Connection,
    candidates: Sequence[tuple[str, str, str | None, str | None]],
    *,
    received_at: str,
) -> int:
    machine_id: str | None = None
    logged = 0
    for table_name, row_pk, _updated_at, _origin_device_id in candidates:
        live = _fetch_live_stamp(conn, table_name, row_pk)
        if live is None:
            continue
        live_updated_at, live_origin = live
        changelog_stamp = _latest_changelog_stamp(conn, table_name, row_pk)
        if changelog_stamp is not None and str(changelog_stamp) == str(live_updated_at):
            continue
        stamp_value = str(live_updated_at) if live_updated_at is not None else sync_stamp.FLOOR_STAMP
        if live_origin is None or str(live_origin) == NO_ORIGIN:
            if machine_id is None:
                machine_id = sync_stamp.ensure_local_machine(conn)
            origin_value = machine_id
        else:
            origin_value = str(live_origin)
        conn.execute(
            f"INSERT INTO {HUB_CHANGELOG_TABLE}("
            "table_name, row_pk, updated_at, origin_device_id, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (table_name, row_pk, stamp_value, origin_value, received_at),
        )
        logged += 1
    return logged


def repair_hub_changelog_stamps(
    conn: sqlite3.Connection,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    """Re-offer hub changelog rows whose latest stamp disagrees with live rows."""
    if not machine_identity.is_hub_from_env(env):
        return 0
    if not schema_markers.table_exists(conn, schema_markers.MARKER_TABLE):
        return 0
    if schema_markers.has_marker(conn, REPAIR_MARKER):
        return 0

    candidates = _scan_stamp_mismatch_candidates(conn)
    received_at = sync_stamp.canonical_now()

    with sync_stamp.stamped_transaction(conn):
        if schema_markers.has_marker(conn, REPAIR_MARKER):
            return 0
        logged = _append_repair_changelog_rows(
            conn, candidates, received_at=received_at
        )
        schema_markers.insert_marker(conn, REPAIR_MARKER)
    return logged


__all__ = [
    "HUB_CHANGELOG_TABLE",
    "REPAIR_MARKER",
    "_V17",
    "repair_hub_changelog_stamps",
]
