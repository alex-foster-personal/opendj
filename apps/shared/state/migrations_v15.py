"""Migration 14 -> 15: backfill legacy ``track_fields`` sync stamps (#3101, #3136).

Rows that predate migration v6 carry NULL ``updated_at`` while still holding
a usable ``modified_at`` from the rekordbox import. CloudSync orders those
rows by ``modified_at`` until this one-time backfill copies it into
``updated_at``; already-stamped rows are untouched.

Stamp backfills are changelog-producing writes (issue #3136): every row
touched must appear in the active-role changelog -- ``hub_changelog`` when
``MDT_IS_HUB=1``, else ``local_changelog`` -- so the existing incremental
pull and push fences can deliver it. Role comes from the launch contract, not
from the replicated ``machines.is_hub`` row.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from . import machine_identity
from . import schema_markers
from . import sync_stamp

HUB_CHANGELOG_TABLE: str = "hub_changelog"
_TRACK_FIELDS: str = "track_fields"
V15_BACKFILL_MARKER: str = "track_fields_stamp_backfill_v1"

# v15 has no standalone SQL: the Python backfill owns row updates and changelog
# appends atomically (issue #3136).
_V15: list[str] = []


def active_changelog_table(
    env: Mapping[str, str] | None = None,
) -> str:
    """Return ``hub_changelog`` or ``local_changelog`` for this process role."""
    if machine_identity.is_hub_from_env(env):
        return HUB_CHANGELOG_TABLE
    return sync_stamp.LOCAL_CHANGELOG_TABLE


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def log_stamp_backfill_rows(
    conn: sqlite3.Connection,
    *,
    table: str,
    row_pks: Sequence[tuple[Any, ...]],
    stamp_by_pk: Mapping[tuple[Any, ...], str],
    origin_by_pk: Mapping[tuple[Any, ...], str | None],
    env: Mapping[str, str] | None = None,
) -> int:
    """Append active-role changelog rows for a stamp backfill (reusable primitive).

    ``origin_by_pk`` values are written to the changelog only; domain rows are
    not updated here. When a row's origin is ``None``, the current machine id
    is used in the changelog entry without mutating the domain row.
    """
    if not row_pks:
        return 0
    changelog_table = active_changelog_table(env)
    if not _table_exists(conn, changelog_table):
        return 0
    machine_id: str | None = None
    received_at = sync_stamp.canonical_now()
    logged = 0
    encoded_pks = {pk: sync_stamp.encode_row_pk(pk) for pk in row_pks}
    existing = {
        str(row[0])
        for row in conn.execute(
            f"SELECT row_pk FROM {changelog_table} "
            "WHERE table_name = ? AND row_pk IN ({})".format(
                ", ".join("?" for _ in row_pks)
            ),
            (table, *encoded_pks.values()),
        ).fetchall()
    }
    for pk in row_pks:
        encoded_pk = encoded_pks[pk]
        if encoded_pk in existing:
            continue
        stamp = stamp_by_pk[pk]
        origin = origin_by_pk.get(pk)
        if origin is None:
            if machine_id is None:
                machine_id = sync_stamp.ensure_local_machine(conn)
            origin_value = machine_id
        else:
            origin_value = origin
        conn.execute(
            f"INSERT INTO {changelog_table}("
            "table_name, row_pk, updated_at, origin_device_id, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (table, encoded_pk, stamp, origin_value, received_at),
        )
        logged += 1
    return logged


def _scan_track_fields_backfill_candidates(
    conn: sqlite3.Connection,
    *,
    env: Mapping[str, str] | None = None,
) -> list[tuple[Any, ...]]:
    changelog_table = active_changelog_table(env)
    if not _table_exists(conn, changelog_table):
        return []
    rows = conn.execute(
        f"""
        SELECT stable_id, field_name
        FROM {_TRACK_FIELDS}
        WHERE modified_at IS NOT NULL
          AND (
            updated_at IS NULL
            OR (
              updated_at = modified_at
              AND NOT EXISTS (
                SELECT 1 FROM {changelog_table} c
                WHERE c.table_name = ?
                  AND c.row_pk = json_array(stable_id, field_name)
              )
            )
          )
        """,
        (_TRACK_FIELDS,),
    ).fetchall()
    return [(stable_id, field_name) for stable_id, field_name in rows]


def _apply_track_fields_stamp_backfill(
    conn: sqlite3.Connection,
    *,
    env: Mapping[str, str] | None = None,
    candidates: Sequence[tuple[Any, ...]] | None = None,
) -> int:
    """Stamp legacy ``track_fields`` rows and log them to the active changelog."""
    if candidates is None:
        candidates = _scan_track_fields_backfill_candidates(conn, env=env)
    if not candidates:
        return 0

    touched = 0
    to_log: list[tuple[Any, ...]] = []
    stamp_by_pk: dict[tuple[Any, ...], str] = {}
    origin_by_pk: dict[tuple[Any, ...], str | None] = {}

    for stable_id, field_name in candidates:
        row = conn.execute(
            "SELECT modified_at, updated_at, origin_device_id "
            f"FROM {_TRACK_FIELDS} WHERE stable_id = ? AND field_name = ?",
            (stable_id, field_name),
        ).fetchone()
        if row is None:
            continue
        modified_at, updated_at, origin = row
        pk = (stable_id, field_name)
        if updated_at is None:
            conn.execute(
                f"UPDATE {_TRACK_FIELDS} SET updated_at = modified_at "
                "WHERE stable_id = ? AND field_name = ? AND updated_at IS NULL",
                (stable_id, field_name),
            )
            stamp = str(modified_at)
            touched += 1
        elif str(updated_at) == str(modified_at):
            stamp = str(updated_at)
        else:
            continue
        to_log.append(pk)
        stamp_by_pk[pk] = stamp
        origin_by_pk[pk] = origin

    touched += log_stamp_backfill_rows(
        conn,
        table=_TRACK_FIELDS,
        row_pks=to_log,
        stamp_by_pk=stamp_by_pk,
        origin_by_pk=origin_by_pk,
        env=env,
    )
    return touched


def backfill_track_fields_stamps(
    conn: sqlite3.Connection,
    *,
    env: Mapping[str, str] | None = None,
    transactional: bool = True,
) -> int:
    """Run the v15 track_fields stamp backfill and one-shot changelog re-offer.

    When ``transactional`` is false the caller already holds a write
    transaction (``apply_migrations`` for step 15). Otherwise the whole pass
    is wrapped in :func:`sync_stamp.stamped_transaction`.
    """
    if schema_markers.table_exists(conn, schema_markers.MARKER_TABLE):
        if schema_markers.has_marker(conn, V15_BACKFILL_MARKER):
            return 0

    candidates = _scan_track_fields_backfill_candidates(conn, env=env)

    def _run() -> int:
        if schema_markers.table_exists(conn, schema_markers.MARKER_TABLE):
            if schema_markers.has_marker(conn, V15_BACKFILL_MARKER):
                return 0
        touched = _apply_track_fields_stamp_backfill(
            conn, env=env, candidates=candidates
        )
        if schema_markers.table_exists(conn, schema_markers.MARKER_TABLE):
            schema_markers.insert_marker(conn, V15_BACKFILL_MARKER)
        return touched

    if transactional:
        with sync_stamp.stamped_transaction(conn):
            return _run()
    return _run()


__all__ = [
    "_V15",
    "HUB_CHANGELOG_TABLE",
    "V15_BACKFILL_MARKER",
    "active_changelog_table",
    "backfill_track_fields_stamps",
    "log_stamp_backfill_rows",
]
