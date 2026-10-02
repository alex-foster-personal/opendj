"""The adversarial scenario verbs (plan W9): tombstones, clocks, restores, faults.

ADR-0007 designates the YAML scenarios as the tier that carries over to fleet
mode, and before this module they could express only edit, sync, partition
and two assertions. These verbs are what an adversarial round needs:

* ``delete`` / ``reinsert`` -- soft-delete a live row, or reactivate a
  tombstoned one, stamped and logged the way a real writer does.
* ``skew_clock`` -- offset one machine's logical clock by ``seconds``; every
  later auto-stamped edit on that machine carries the skew.
* ``restore_hub`` -- ``op: snapshot`` copies the hub DB aside under a name,
  ``op: restore`` puts that copy back, exactly as a restore from backup does:
  the DB moves backwards and the generation anchor on disk does not.
* ``prune`` -- run the real changelog retention on the hub (``hub_changelog``)
  or a spoke (``local_changelog``), optionally ``advance_days`` into the
  future so an offline-past-retention case is expressible.
* ``fault`` -- arm a :class:`~tests.cloudsync.fault_transport.FaultTransport`
  fault on one spoke's next calls. The sync it breaks must say
  ``expect_error: transport``, and a fault still armed when the scenario
  ends fails the run, because that scenario never exercised what it names.
* ``rename_machine`` -- the name a spoke syncs under from now on.

It also edits the two widened tables that need their own writer:
``track_fields`` (an upsert of one field) and ``track_locations`` (an upsert
of the acting machine's OWN location, ADR 08 point 1). Imports only
``scenario_runner_common`` and ``fault_transport``, so the three-way split of
the harness stays acyclic (see ``scenario_runner_common``'s docstring).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from apps.shared.state import sync_stamp
from apps.sync_hub import engine, protocol

from .scenario_runner_common import (
    _ADVERSARIAL_ACTIONS,
    ScenarioError,
    SimRun,
    Step,
    _ensure_spoke,
    _log_edit,
    _require,
    _require_mapping,
    _resolve_pk,
    _stamp,
    _validate_editable_columns,
    client,
)

_FAULT_PATHS: frozenset[str] = frozenset({"hello", "push", "pull", "digest"})
_INJECTABLE_FAULT_MODES: tuple[str, ...] = ("fail_before_send", "drop_response_after_commit")
_SPOKE_ONLY_ACTIONS: frozenset[str] = frozenset(
    {"delete", "reinsert", "skew_clock", "fault", "rename_machine"}
)

Handler = Callable[[SimRun, str, Mapping[str, Any]], None]


# ----- helpers -------------------------------------------------------------


def _synced_pk(
    run: SimRun, table: str, args: Mapping[str, Any], machine: str
) -> tuple[dict[str, str], tuple[str, ...]]:
    """The resolved pk of a synced LWW row, as a mapping and in spec order."""
    spec = protocol.SPEC_BY_TABLE.get(table)
    if spec is None:
        raise ScenarioError(
            f"{table!r} is not a synced LWW table; tombstones apply only to "
            f"{sorted(protocol.SPEC_BY_TABLE)}"
        )
    raw = _require_mapping(_require(args, "pk", where=table), where=f"{table}.pk")
    pk = _resolve_pk(run, table, raw, self_machine=machine)
    return pk, tuple(pk[column] for column in spec.pk)


def _where(pk: Mapping[str, str]) -> str:
    return " AND ".join(f"{column} = ?" for column in pk)


def _require_number(args: Mapping[str, Any], key: str, *, where: str) -> float:
    value = _require(args, key, where=where)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ScenarioError(f"{where}.{key} must be a number, got {value!r}")
    return float(value)


def _copy_db(source: Path, target: Path) -> None:
    """A consistent copy through SQLite's online backup API (WAL-safe)."""
    source_conn = sqlite3.connect(source)
    target_conn = sqlite3.connect(target)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()


# ----- tombstones ------------------------------------------------------------


def _apply_delete(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    table = str(_require(args, "table", where="delete"))
    pk, pk_tuple = _synced_pk(run, table, args, machine)
    origin = run.machine_ids[machine]
    conn = run.conn(machine)
    try:
        stamp = _log_edit(conn, table, pk_tuple, origin, _stamp(args, run, machine))
        cursor = conn.execute(
            f"UPDATE {table} SET deleted_at = ?, updated_at = ?, origin_device_id = ? "
            f"WHERE {_where(pk)} AND deleted_at IS NULL",
            (stamp, stamp, origin, *pk.values()),
        )
        if cursor.rowcount == 0:
            raise ScenarioError(
                f"delete: no LIVE {table} row {pk} (missing, or already tombstoned)"
            )
    finally:
        conn.close()


def _apply_reinsert(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    table = str(_require(args, "table", where="reinsert"))
    pk, pk_tuple = _synced_pk(run, table, args, machine)
    changes = _require_mapping(args.get("set") or {}, where="reinsert.set")
    origin = run.machine_ids[machine]
    conn = run.conn(machine)
    try:
        _validate_editable_columns(conn, table, changes)
        stamp = _log_edit(conn, table, pk_tuple, origin, _stamp(args, run, machine))
        assignments = "".join(f"{column} = ?, " for column in changes)
        restore: tuple[str, ...] = ()
        if table == "tracks":
            # The explicit restore, as StateWriter.undelete_track writes it:
            # without restored_at a live row never outranks a tombstone.
            assignments += "restored_at = ?, deleted_reason = NULL, "
            restore = (stamp,)
        cursor = conn.execute(
            f"UPDATE {table} SET {assignments}deleted_at = NULL, updated_at = ?, "
            f"origin_device_id = ? WHERE {_where(pk)} AND deleted_at IS NOT NULL",
            (*changes.values(), *restore, stamp, origin, *pk.values()),
        )
        if cursor.rowcount == 0:
            raise ScenarioError(f"reinsert: no TOMBSTONED {table} row {pk}")
    finally:
        conn.close()


# ----- clocks, names and faults --------------------------------------------


def _apply_skew_clock(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    run.clock_skew_s[machine] = _require_number(args, "seconds", where="skew_clock")


def _apply_rename_machine(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    name = str(_require(args, "name", where="rename_machine")).strip()
    if not name:
        raise ScenarioError("rename_machine.name must not be blank")
    run.names[machine] = name


def _apply_fault(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    mode = str(_require(args, "mode", where="fault"))
    if mode not in _INJECTABLE_FAULT_MODES:
        raise ScenarioError(
            f"fault.mode must be one of {list(_INJECTABLE_FAULT_MODES)}, got {mode!r}"
        )
    path = str(_require(args, "path", where="fault"))
    if path not in _FAULT_PATHS:
        raise ScenarioError(f"fault.path must be one of {sorted(_FAULT_PATHS)}, got {path!r}")
    nth = args.get("nth", 1)
    if isinstance(nth, bool) or not isinstance(nth, int) or nth < 1:
        raise ScenarioError(f"fault.nth must be a positive integer, got {nth!r}")
    run.transport_for(machine).fail_on_nth(path, nth, mode=mode)  # type: ignore[arg-type]


def assert_faults_spent(run: SimRun) -> None:
    """A fault still armed at the end means the scenario never hit its failure."""
    pending = {machine: t.pending for machine, t in run.transports.items() if t.pending}
    if pending:
        raise ScenarioError(
            f"armed fault(s) never fired, so this scenario never exercised the "
            f"failure it names: {pending}"
        )


# ----- the hub's own maintenance -------------------------------------------


def _apply_restore_hub(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    if machine != run.scenario.hub.id:
        raise ScenarioError(
            f"restore_hub runs on the hub ({run.scenario.hub.id!r}), not {machine!r}"
        )
    op = str(_require(args, "op", where="restore_hub"))
    name = str(_require(args, "name", where="restore_hub"))
    hub_db = client.state_db_path(run.data_dirs[machine])
    if op == "snapshot":
        if name in run.hub_snapshots:
            raise ScenarioError(f"restore_hub: snapshot {name!r} already taken")
        target = run.data_dirs[machine].parent / f"{machine}-snapshot-{name}.db"
        _copy_db(hub_db, target)
        run.hub_snapshots[name] = target
    elif op == "restore":
        if name not in run.hub_snapshots:
            raise ScenarioError(
                f"restore_hub: no snapshot {name!r} (taken: {sorted(run.hub_snapshots)})"
            )
        _copy_db(run.hub_snapshots[name], hub_db)
    else:
        raise ScenarioError(f"restore_hub.op must be 'snapshot' or 'restore', got {op!r}")


def _apply_prune(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    default_changelog = (
        engine.HUB_CHANGELOG_TABLE
        if machine == run.scenario.hub.id
        else sync_stamp.LOCAL_CHANGELOG_TABLE
    )
    changelog = str(args.get("changelog") or default_changelog)
    keep_days = _require_number(args, "keep_days", where="prune")
    keep_rows = int(_require_number(args, "keep_rows", where="prune"))
    advance_days = float(args.get("advance_days", 0))
    now = sync_stamp.canonical_from(datetime.now(UTC) + timedelta(days=advance_days))
    conn = run.conn(machine)
    try:
        conn.execute("BEGIN")
        try:
            deleted = engine.prune_changelog(
                conn, changelog=changelog, keep_days=keep_days, keep_rows=keep_rows, now=now
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()
    wanted = args.get("expect_deleted")
    if wanted is not None and deleted != wanted:
        raise ScenarioError(f"prune deleted {deleted} {changelog} entries, expected {wanted}")


# ----- the widened editable tables -----------------------------------------


def _require_track(conn: sqlite3.Connection, stable_id: str, where: str) -> None:
    if conn.execute("SELECT 1 FROM tracks WHERE stable_id = ?", (stable_id,)).fetchone() is None:
        raise ScenarioError(f"{where}: no track {stable_id!r} -- seed it first")


def _edit_track_field(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    stable_id = str(_require(args, "stable_id", where="edit track_fields"))
    field_name = str(_require(args, "field_name", where="edit track_fields"))
    if "value" not in args:
        raise ScenarioError("edit track_fields needs 'value'")
    value_json = json.dumps(args["value"], sort_keys=True)
    source = str(args.get("source", "manual"))
    origin = run.machine_ids[machine]
    conn = run.conn(machine)
    try:
        _require_track(conn, stable_id, "edit track_fields")
        stamp = _log_edit(
            conn, "track_fields", (stable_id, field_name), origin, _stamp(args, run, machine)
        )
        conn.execute(
            """
            INSERT INTO track_fields(
                stable_id, field_name, value_json, source, modified_at,
                updated_at, origin_device_id, deleted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(stable_id, field_name) DO UPDATE SET
                value_json       = excluded.value_json,
                source           = excluded.source,
                modified_at      = excluded.modified_at,
                updated_at       = excluded.updated_at,
                origin_device_id = excluded.origin_device_id,
                deleted_at       = NULL
            """,
            (stable_id, field_name, value_json, source, stamp, stamp, origin),
        )
    finally:
        conn.close()


def _edit_track_location(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    location_id = str(_require(args, "location_id", where="edit track_locations"))
    stable_id = str(_require(args, "stable_id", where="edit track_locations"))
    file_path = str(_require(args, "file_path", where="edit track_locations"))
    available = 1 if args.get("available", True) else 0
    origin = run.machine_ids[machine]
    conn = run.conn(machine)
    try:
        _require_track(conn, stable_id, "edit track_locations")
        owner = conn.execute(
            "SELECT machine_id FROM track_locations WHERE location_id = ?", (location_id,)
        ).fetchone()
        if owner is not None and owner[0] != origin:
            raise ScenarioError(
                f"edit track_locations: {location_id!r} belongs to machine {owner[0]}; "
                f"a machine writes only its own locations (ADR 08 point 1)"
            )
        stamp = _log_edit(
            conn, "track_locations", (location_id,), origin, _stamp(args, run, machine)
        )
        conn.execute(
            """
            INSERT INTO track_locations(
                location_id, stable_id, machine_id, kind, role, file_path,
                available, created_at, updated_at, origin_device_id, deleted_at
            )
            VALUES (?, ?, ?, 'local', 'primary', ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(location_id) DO UPDATE SET
                file_path        = excluded.file_path,
                available        = excluded.available,
                updated_at       = excluded.updated_at,
                origin_device_id = excluded.origin_device_id,
                deleted_at       = NULL
            """,
            (location_id, stable_id, origin, file_path, available, stamp, stamp, origin),
        )
    finally:
        conn.close()


# ----- dispatch --------------------------------------------------------------

_HANDLERS: dict[str, Handler] = {
    "delete": _apply_delete,
    "reinsert": _apply_reinsert,
    "skew_clock": _apply_skew_clock,
    "restore_hub": _apply_restore_hub,
    "prune": _apply_prune,
    "fault": _apply_fault,
    "rename_machine": _apply_rename_machine,
}
if frozenset(_HANDLERS) != _ADVERSARIAL_ACTIONS:
    raise RuntimeError(
        f"adversarial handlers {sorted(_HANDLERS)} and the parser's action set "
        f"{sorted(_ADVERSARIAL_ACTIONS)} disagree; a validated step would have no handler"
    )

_EDITORS: dict[str, Handler] = {
    "track_fields": _edit_track_field,
    "track_locations": _edit_track_location,
}
ADVERSARIAL_EDIT_TABLES: frozenset[str] = frozenset(_EDITORS)


def dispatch_adversarial(run: SimRun, step: Step) -> None:
    if step.action in _SPOKE_ONLY_ACTIONS:
        _ensure_spoke(run, step.on, step.action)
    _HANDLERS[step.action](run, step.on, step.args)


def edit_adversarial_table(run: SimRun, machine: str, table: str, args: Mapping[str, Any]) -> None:
    _EDITORS[table](run, machine, args)


__all__ = [
    "ADVERSARIAL_EDIT_TABLES",
    "assert_faults_spent",
    "dispatch_adversarial",
    "edit_adversarial_table",
]
