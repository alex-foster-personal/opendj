"""Migration 16 -> 17: hub changelog stamp repair (#3171).

v17 carries no SQL. Its only job is to version the one-shot hub-side repair
that re-offers rows whose latest ``hub_changelog`` stamp disagrees with the
live domain row, so ``schema_meta`` records which databases have been through
it. v16 (#3165) already supplies both things the repair needs: the
``schema_meta_markers`` table and the ``hub_changelog(table_name, row_pk)``
index.

The repair exists because a legacy hub-side backfill logged 23,416
``track_fields`` changelog rows (and 7,917 ``track_vendor_ids`` rows) carrying
the EPOCH comparison sentinel instead of the row's own ``updated_at``. The
sentinel loses every LWW comparison, so a spoke past those sequences keeps its
stale value permanently (#3057).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence

from apps.sync_hub.engine_common import _pk_predicate
from apps.sync_hub.protocol import SPEC_BY_TABLE, SYNC_TABLES, decode_row_pk
from apps.sync_hub.protocol_common import NO_ORIGIN

from . import machine_identity, schema_markers, sync_stamp

HUB_CHANGELOG_TABLE: str = "hub_changelog"
REPAIR_MARKER: str = "cloudsync_hub_changelog_stamp_repair_v1"

# No standalone SQL, for the same reason v15 has none: the Python repair owns
# the work. A `(table_name, row_pk, seq)` index was measured here and removed
# -- at live-hub scale (31,333 sentinel rows) the run took 0.08 s to scan and
# 0.34 s under `BEGIN IMMEDIATE` with it, and 0.08 s / 0.27 s with it dropped,
# so v16's `(table_name, row_pk)` index already serves both queries and a
# second overlapping index would only cost write time.
_V17: list[str] = []


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
        stamp_value = (
            str(live_updated_at)
            if live_updated_at is not None
            else sync_stamp.FLOOR_STAMP
        )
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
    # Marker first, role second, matching the v15 backfill. Reading the role
    # first would let a malformed MDT_IS_HUB raise out of apply_migrations and
    # abort every open, where the contract is that an unreadable hub flag
    # surfaces as a declared 500 from the route that needs it
    # (tests/cloudsync/test_cloudsync_ops_routes.py).
    if not schema_markers.table_exists(conn, schema_markers.MARKER_TABLE):
        return 0
    if schema_markers.has_marker(conn, REPAIR_MARKER):
        return 0
    try:
        is_hub = machine_identity.is_hub_from_env(env)
    except machine_identity.MachineIdentityError:
        # An unreadable MDT_IS_HUB is a real error, and it is not this
        # function's to raise. Every open_rw runs this hook, so raising here
        # would take down ordinary database opens on a machine whose flag is
        # typo'd, while the contract is that an illegible flag surfaces as a
        # declared 500 from the route that needs the role
        # (CLOUDSYNC_HUB_FLAG_INVALID). Nothing is swallowed: the value is
        # still rejected loudly there, no marker is written here, and the
        # repair runs on the next open once the flag is legible.
        return 0
    if not is_hub:
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
