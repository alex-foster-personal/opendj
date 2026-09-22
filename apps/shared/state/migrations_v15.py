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

Completion is recorded in ``schema_meta_markers`` (v16) so repeat opens do
not rescan ``track_fields`` or hold a writer lock across O(N) changelog
probes (issue #3165).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from . import machine_identity, sync_stamp

HUB_CHANGELOG_TABLE: str = "hub_changelog"
_TRACK_FIELDS: str = "track_fields"
MARKER_TABLE: str = "schema_meta_markers"
TRACK_FIELDS_STAMP_BACKFILL_MARKER: str = "v15_track_fields_stamp_backfill"

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


def _marker_table_exists(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, MARKER_TABLE)


def _backfill_marker_present(conn: sqlite3.Connection) -> bool:
    if not _marker_table_exists(conn):
        return False
    return (
        conn.execute(
            f"SELECT 1 FROM {MARKER_TABLE} WHERE marker = ? LIMIT 1",
            (TRACK_FIELDS_STAMP_BACKFILL_MARKER,),
        ).fetchone()
        is not None
    )


def _write_backfill_marker(conn: sqlite3.Connection) -> None:
    if not _marker_table_exists(conn):
        return
    conn.execute(
        f"INSERT INTO {MARKER_TABLE}(marker, applied_at) VALUES (?, ?)",
        (TRACK_FIELDS_STAMP_BACKFILL_MARKER, sync_stamp.canonical_now()),
    )


def _changelog_has_row(
    conn: sqlite3.Connection,
    changelog_table: str,
    table: str,
    encoded_pk: str,
) -> bool:
    return (
        conn.execute(
            f"SELECT 1 FROM {changelog_table} "
            "WHERE table_name = ? AND row_pk = ? LIMIT 1",
            (table, encoded_pk),
        ).fetchone()
        is not None
    )


def _select_backfill_candidates(
    conn: sqlite3.Connection,
    changelog_table: str,
) -> list[tuple[Any, ...]]:
    """Return rows needing stamp and/or changelog append via one set-based read."""
    rows = conn.execute(
        f"""
        SELECT tf.stable_id,
               tf.field_name,
               tf.modified_at,
               tf.updated_at,
               tf.origin_device_id,
               (tf.updated_at IS NULL) AS needs_stamp,
               (c.row_pk IS NULL) AS needs_log
        FROM {_TRACK_FIELDS} tf
        LEFT JOIN {changelog_table} c
          ON c.table_name = ?
         AND c.row_pk = json_array(tf.stable_id, tf.field_name)
        WHERE tf.modified_at IS NOT NULL
          AND (
            tf.updated_at IS NULL
            OR (tf.updated_at = tf.modified_at AND c.row_pk IS NULL)
          )
        """,
        (_TRACK_FIELDS,),
    ).fetchall()
    return list(rows)


def log_stamp_backfill_rows(
    conn: sqlite3.Connection,
    *,
    table: str,
    row_pks: Sequence[tuple[Any, ...]],
    stamp_by_pk: Mapping[tuple[Any, ...], str],
    origin_by_pk: Mapping[tuple[Any, ...], str | None],
    env: Mapping[str, str] | None = None,
    skip_duplicate_check: bool = False,
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
    for pk in row_pks:
        encoded_pk = sync_stamp.encode_row_pk(pk)
        if not skip_duplicate_check and _changelog_has_row(
            conn, changelog_table, table, encoded_pk
        ):
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


def _apply_track_fields_stamp_backfill(
    conn: sqlite3.Connection,
    candidates: Sequence[tuple[Any, ...]],
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    """Apply a pre-selected candidate set inside an existing write transaction."""
    if _backfill_marker_present(conn):
        return 0

    changelog_table = active_changelog_table(env)
    if not _table_exists(conn, changelog_table):
        return 0

    if not candidates:
        _write_backfill_marker(conn)
        return 0

    touched = 0
    to_log: list[tuple[Any, ...]] = []
    stamp_by_pk: dict[tuple[Any, ...], str] = {}
    origin_by_pk: dict[tuple[Any, ...], str | None] = {}

    for (
        stable_id,
        field_name,
        modified_at,
        updated_at,
        origin,
        needs_stamp,
        needs_log,
    ) in candidates:
        pk = (stable_id, field_name)
        if needs_stamp:
            conn.execute(
                f"UPDATE {_TRACK_FIELDS} SET updated_at = modified_at "
                "WHERE stable_id = ? AND field_name = ? AND updated_at IS NULL",
                (stable_id, field_name),
            )
            touched += 1
        if not needs_log:
            continue
        stamp = str(modified_at if needs_stamp else updated_at)
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
        skip_duplicate_check=True,
    )
    _write_backfill_marker(conn)
    return touched


def _finalize_empty_backfill(conn: sqlite3.Connection, *, transactional: bool) -> int:
    if not _marker_table_exists(conn):
        return 0
    if transactional:
        with sync_stamp.stamped_transaction(conn):
            if _backfill_marker_present(conn):
                return 0
            _write_backfill_marker(conn)
    elif _backfill_marker_present(conn):
        return 0
    else:
        _write_backfill_marker(conn)
    return 0


def backfill_track_fields_stamps(
    conn: sqlite3.Connection,
    *,
    env: Mapping[str, str] | None = None,
    transactional: bool = True,
) -> int:
    """Run the v15 track_fields stamp backfill and one-shot changelog re-offer.

    When ``transactional`` is false the caller already holds a write
    transaction (``apply_migrations`` for step 15). Otherwise the candidate
    read runs outside the lock; only the apply phase uses
    :func:`sync_stamp.stamped_transaction`.
    """
    if _backfill_marker_present(conn):
        return 0

    changelog_table = active_changelog_table(env)
    if not _table_exists(conn, changelog_table):
        return 0

    candidates = _select_backfill_candidates(conn, changelog_table)

    if not candidates:
        return _finalize_empty_backfill(conn, transactional=transactional)

    if transactional:
        with sync_stamp.stamped_transaction(conn):
            if _backfill_marker_present(conn):
                return 0
            return _apply_track_fields_stamp_backfill(conn, candidates, env=env)
    return _apply_track_fields_stamp_backfill(conn, candidates, env=env)


__all__ = [
    "_V15",
    "HUB_CHANGELOG_TABLE",
    "MARKER_TABLE",
    "TRACK_FIELDS_STAMP_BACKFILL_MARKER",
    "active_changelog_table",
    "backfill_track_fields_stamps",
    "log_stamp_backfill_rows",
]
