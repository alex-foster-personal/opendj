"""Scenario seeding and step execution: the YAML verbs, as real writes.

Split out of :mod:`tests.cloudsync.scenario_runner` (quality-gate file_size
ratchet, round 4): the schema, sim-mode transport, ``SimRun`` and the small
write-path validators live in :mod:`tests.cloudsync.scenario_runner_common`;
this module is everything that turns a parsed
:class:`~tests.cloudsync.scenario_runner_common.Scenario`'s ``seed`` block
and ``steps`` list into real rows in real per-machine state DBs. Imported
back into ``scenario_runner`` for ``run_scenario_sim``. This module imports
only from ``scenario_runner_common``, never from ``scenario_runner`` itself
-- see that common module's docstring for why the three-way split has to
stay acyclic (``python -m tests.cloudsync.scenario_runner`` re-imports
``scenario_runner`` under its real name, and a real cycle between two of its
own submodules resolves one of them partially initialized).
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from apps.sync_hub import protocol

from .fault_transport import FaultInjected
from .scenario_runner_adversarial import (
    ADVERSARIAL_EDIT_TABLES,
    assert_faults_spent,
    dispatch_adversarial,
    edit_adversarial_table,
)
from .scenario_runner_common import (
    _ADVERSARIAL_ACTIONS,
    _EDITABLE_TABLES,
    _SEED_ORIGIN,
    _SYNC_RESULT_FIELDS,
    ScenarioError,
    SimRun,
    Step,
    _ensure_spoke,
    _log_edit,
    _require,
    _require_mapping,
    _require_nonempty_list,
    _resolve_pk,
    _stamp,
    _validate_editable_columns,
    client,
)

# ----- seeding ---------------------------------------------------------------


def _seed_track(run: SimRun, entry: Mapping[str, Any]) -> None:
    entry = _require_mapping(entry, where="seed.tracks[]")
    stable_id = str(_require(entry, "stable_id", where="seed.tracks[]"))
    on = _require_nonempty_list(entry.get("on"), where=f"seed.tracks[{stable_id}].on")
    updated_at = str(entry.get("updated_at") or run.next_tick())
    title = entry.get("title")
    stable_id_tier = str(entry.get("stable_id_tier", "inferred"))
    for machine in on:
        conn = run.conn(str(machine))
        try:
            conn.execute(
                """
                INSERT INTO tracks(
                    stable_id, stable_id_tier, title, content_hash, created_at,
                    updated_at, origin_device_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_id,
                    stable_id_tier,
                    title,
                    hashlib.sha256(stable_id.encode("utf-8")).hexdigest(),
                    updated_at,
                    updated_at,
                    _SEED_ORIGIN,
                ),
            )
        finally:
            conn.close()


def _seed_playlist(run: SimRun, entry: Mapping[str, Any]) -> None:
    entry = _require_mapping(entry, where="seed.playlists[]")
    playlist_id = str(_require(entry, "playlist_id", where="seed.playlists[]"))
    on = _require_nonempty_list(entry.get("on"), where=f"seed.playlists[{playlist_id}].on")
    name = str(_require(entry, "name", where=f"seed.playlists[{playlist_id}]"))
    updated_at = str(entry.get("updated_at") or run.next_tick())
    members = entry.get("members")
    if members is not None and not isinstance(members, list):
        raise ScenarioError(f"seed.playlists[{playlist_id}].members must be a list")
    for machine in on:
        conn = run.conn(str(machine))
        try:
            conn.execute(
                """
                INSERT INTO playlists(
                    playlist_id, name, vendor, vendor_pl_id, created_at,
                    updated_at, origin_device_id
                )
                VALUES (?, ?, 'open-dj', ?, ?, ?, ?)
                """,
                (playlist_id, name, playlist_id, updated_at, updated_at, _SEED_ORIGIN),
            )
            for position, stable_id in enumerate(members or []):
                conn.execute(
                    """
                    INSERT INTO playlist_memberships(
                        playlist_id, stable_id, position, updated_at,
                        origin_device_id
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (playlist_id, str(stable_id), position, updated_at, _SEED_ORIGIN),
                )
        finally:
            conn.close()


def _write_policy(
    run: SimRun,
    machine: str,
    *,
    kind: str,
    mode: str,
    updated_at: str,
    asset_kind: str | None = None,
    playlist_id: str | None = None,
    cache_budget_mb: int | None = None,
) -> None:
    """Upsert one self-referential policy row -- ``machine_id`` is always
    ``machine``'s own real id, both as the FK and as the origin, because a
    machine only ever writes its own policy (D5)."""
    origin = run.machine_ids[machine]
    conn = run.conn(machine)
    try:
        if kind == "sync_policy":
            if asset_kind is None:
                raise ScenarioError("sync_policy needs 'asset_kind'")
            updated_at = _log_edit(
                conn, "sync_policies", (origin, asset_kind), origin, updated_at
            )
            conn.execute(
                """
                INSERT INTO sync_policies(
                    machine_id, asset_kind, mode, cache_budget_mb,
                    updated_at, origin_device_id
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(machine_id, asset_kind) DO UPDATE SET
                    mode             = excluded.mode,
                    cache_budget_mb  = excluded.cache_budget_mb,
                    updated_at       = excluded.updated_at,
                    origin_device_id = excluded.origin_device_id
                """,
                (origin, asset_kind, mode, cache_budget_mb, updated_at, origin),
            )
        elif kind == "playlist_pin":
            if playlist_id is None:
                raise ScenarioError("playlist_pin needs 'playlist_id'")
            updated_at = _log_edit(
                conn, "playlist_pins", (origin, playlist_id), origin, updated_at
            )
            conn.execute(
                """
                INSERT INTO playlist_pins(
                    machine_id, playlist_id, mode, updated_at, origin_device_id
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(machine_id, playlist_id) DO UPDATE SET
                    mode             = excluded.mode,
                    updated_at       = excluded.updated_at,
                    origin_device_id = excluded.origin_device_id
                """,
                (origin, playlist_id, mode, updated_at, origin),
            )
        else:
            raise ScenarioError(
                f"policy kind must be 'sync_policy' or 'playlist_pin', got {kind!r}"
            )
    finally:
        conn.close()


def _seed_policies(run: SimRun, entries: Sequence[Any]) -> None:
    for entry in entries:
        entry = _require_mapping(entry, where="seed.policies[]")
        kind = str(_require(entry, "kind", where="seed.policies[]"))
        on = _require_nonempty_list(entry.get("on"), where=f"seed.policies[{kind}].on")
        mode = str(_require(entry, "mode", where=f"seed.policies[{kind}]"))
        updated_at = str(entry.get("updated_at") or run.next_tick())
        for machine in on:
            if kind == "sync_policy":
                asset_kind = str(
                    _require(entry, "asset_kind", where="seed.policies[sync_policy]")
                )
                _write_policy(
                    run,
                    str(machine),
                    kind=kind,
                    mode=mode,
                    asset_kind=asset_kind,
                    cache_budget_mb=entry.get("cache_budget_mb"),
                    updated_at=updated_at,
                )
            elif kind == "playlist_pin":
                playlist_id = str(
                    _require(entry, "playlist_id", where="seed.policies[playlist_pin]")
                )
                _write_policy(
                    run,
                    str(machine),
                    kind=kind,
                    mode=mode,
                    playlist_id=playlist_id,
                    updated_at=updated_at,
                )
            else:
                raise ScenarioError(
                    f"seed.policies[{kind}]: kind must be 'sync_policy' or "
                    f"'playlist_pin'"
                )


def _seed(run: SimRun) -> None:
    for entry in run.scenario.seed.get("tracks") or []:
        _seed_track(run, entry)
    for entry in run.scenario.seed.get("playlists") or []:
        _seed_playlist(run, entry)
    _seed_policies(run, run.scenario.seed.get("policies") or [])


# ----- step actions ------------------------------------------------------


def _edit_track(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    stable_id = str(_require(args, "stable_id", where="edit tracks"))
    changes = _require_mapping(_require(args, "set", where="edit tracks"), where="edit tracks.set")
    if not changes:
        raise ScenarioError("edit tracks: 'set' must not be empty")
    conn = run.conn(machine)
    try:
        _validate_editable_columns(conn, "tracks", changes)
        origin = run.machine_ids[machine]
        updated_at = _log_edit(
            conn, "tracks", (stable_id,), origin, _stamp(args, run, machine)
        )
        assignments = ", ".join(f"{col} = ?" for col in changes)
        cursor = conn.execute(
            f"UPDATE tracks SET {assignments}, updated_at = ?, "
            f"origin_device_id = ? WHERE stable_id = ?",
            (*changes.values(), updated_at, origin, stable_id),
        )
        if cursor.rowcount == 0:
            raise ScenarioError(f"edit tracks: no row {stable_id!r} -- seed it first")
    finally:
        conn.close()


def _edit_playlist(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    playlist_id = str(_require(args, "playlist_id", where="edit playlists"))
    changes = args.get("set") or {}
    members = args.get("members")
    if not changes and members is None:
        raise ScenarioError("edit playlists: need 'set' and/or 'members'")
    if members is not None and not isinstance(members, list):
        raise ScenarioError("edit playlists: 'members' must be a list of stable_ids")
    conn = run.conn(machine)
    try:
        origin = run.machine_ids[machine]
        updated_at = _log_edit(
            conn, "playlists", (playlist_id,), origin, _stamp(args, run, machine)
        )
        if changes:
            _validate_editable_columns(conn, "playlists", changes)
            assignments = ", ".join(f"{col} = ?" for col in changes)
            cursor = conn.execute(
                f"UPDATE playlists SET {assignments}, updated_at = ?, "
                f"origin_device_id = ? WHERE playlist_id = ?",
                (*changes.values(), updated_at, origin, playlist_id),
            )
        else:
            cursor = conn.execute(
                "UPDATE playlists SET updated_at = ?, origin_device_id = ? "
                "WHERE playlist_id = ?",
                (updated_at, origin, playlist_id),
            )
        if cursor.rowcount == 0:
            raise ScenarioError(f"edit playlists: no row {playlist_id!r} -- seed it first")
        if members is not None:
            conn.execute(
                "DELETE FROM playlist_memberships WHERE playlist_id = ?", (playlist_id,)
            )
            for position, stable_id in enumerate(members):
                conn.execute(
                    """
                    INSERT INTO playlist_memberships(
                        playlist_id, stable_id, position, updated_at,
                        origin_device_id
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (playlist_id, str(stable_id), position, updated_at, origin),
                )
    finally:
        conn.close()


def _edit_policy(run: SimRun, machine: str, table: str, args: Mapping[str, Any]) -> None:
    mode = str(_require(args, "mode", where=f"edit {table}"))
    updated_at = _stamp(args, run, machine)
    if table == "sync_policies":
        asset_kind = str(_require(args, "asset_kind", where="edit sync_policies"))
        _write_policy(
            run, machine, kind="sync_policy", mode=mode, asset_kind=asset_kind,
            cache_budget_mb=args.get("cache_budget_mb"), updated_at=updated_at,
        )
    else:
        playlist_id = str(_require(args, "playlist_id", where="edit playlist_pins"))
        _write_policy(
            run, machine, kind="playlist_pin", mode=mode, playlist_id=playlist_id,
            updated_at=updated_at,
        )


def _apply_edit(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    table = str(_require(args, "table", where="edit"))
    if table not in _EDITABLE_TABLES:
        raise ScenarioError(
            f"edit: table {table!r} is not one of {sorted(_EDITABLE_TABLES)}"
        )
    if table == "tracks":
        _edit_track(run, machine, args)
    elif table == "playlists":
        _edit_playlist(run, machine, args)
    elif table == "playlist_memberships":
        # Membership has no row of its own on the wire: it rides its
        # playlist row (whole-list replacement), so it is written the same
        # way. Unlike 'reorder', an edit may EMPTY the playlist (members: []).
        _replace_members(run, machine, args, verb="edit playlist_memberships", allow_empty=True)
    elif table in ADVERSARIAL_EDIT_TABLES:
        edit_adversarial_table(run, machine, table, args)
    elif table in ("sync_policies", "playlist_pins"):
        _edit_policy(run, machine, table, args)
    else:
        raise ScenarioError(f"edit: no editor for table {table!r}")


def _apply_reorder(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    """Reorder a playlist: a nonempty whole-list replacement."""
    _replace_members(run, machine, args, verb="reorder", allow_empty=False)


def _replace_members(
    run: SimRun, machine: str, args: Mapping[str, Any], *, verb: str, allow_empty: bool
) -> None:
    """Replace a playlist's whole member list, stamped on its playlist row."""
    _require(args, "playlist_id", where=verb)
    members = args.get("members")
    if allow_empty:
        if not isinstance(members, list):
            raise ScenarioError(
                f"{verb}: 'members' must be a list of stable_ids ([] empties the playlist)"
            )
    elif not allow_empty:
        _require_nonempty_list(members, where=f"{verb}.members")
    if args.get("set"):
        raise ScenarioError(f"{verb} changes membership only; use edit playlists for 'set'")
    _edit_playlist(run, machine, args)


def _apply_sync(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    if machine in run.offline:
        raise ScenarioError(
            "machine is partitioned (offline); a real spoke cannot reach "
            "the hub in this state -- add a 'partition' step to reconnect "
            "it first"
        )
    expect_error = args.get("expect_error")
    if expect_error is not None:
        if expect_error != "transport" or "expect" in args:
            raise ScenarioError(
                "sync.expect_error must be 'transport' and cannot be combined with 'expect'"
            )
        try:
            client.run_sync(
                run.data_dirs[machine],
                "http://hub.invalid",
                transport=run.transport_for(machine),
                name=run.sync_name(machine),
            )
        except FaultInjected:
            return
        raise ScenarioError(
            "sync.expect_error was 'transport' but the sync completed: no armed fault fired"
        )
    result = client.run_sync(
        run.data_dirs[machine],
        "http://hub.invalid",
        transport=run.transport_for(machine),
        name=run.sync_name(machine),
    )
    expect = args.get("expect")
    if expect is None:
        return
    expect = _require_mapping(expect, where="sync.expect")
    for key, wanted in expect.items():
        if key not in _SYNC_RESULT_FIELDS:
            raise ScenarioError(
                f"sync.expect.{key} is not a SyncResult field "
                f"({sorted(_SYNC_RESULT_FIELDS)})"
            )
        got = getattr(result, key)
        if got != wanted:
            raise ScenarioError(f"sync.expect.{key} wanted {wanted!r}, got {got!r}")


def _apply_partition(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    state = str(_require(args, "state", where="partition"))
    if state == "offline":
        run.offline.add(machine)
    elif state == "online":
        run.offline.discard(machine)
    else:
        raise ScenarioError(f"partition.state must be 'offline' or 'online', got {state!r}")


def _apply_assert_converged(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    table = str(_require(args, "table", where="assert_converged"))
    field_name = str(_require(args, "field", where="assert_converged"))
    raw_pk = _require_mapping(
        _require(args, "pk", where="assert_converged"), where="assert_converged.pk"
    )
    if "equals" not in args:
        raise ScenarioError("assert_converged needs 'equals'")
    wanted = args["equals"]
    pk = _resolve_pk(run, table, raw_pk, self_machine=machine)
    conn = run.conn(machine)
    try:
        columns = protocol.table_columns(conn, table)
        if field_name not in columns:
            raise ScenarioError(f"{table} has no column {field_name!r}")
        predicate = " AND ".join(f"{column} = ?" for column in pk)
        row = conn.execute(
            f"SELECT {field_name} FROM {table} WHERE {predicate}", tuple(pk.values())
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ScenarioError(
            f"no {table} row matches {pk} -- expected it to have arrived by this step"
        )
    got = row[0]
    if got != wanted:
        raise ScenarioError(f"{table}.{field_name} for {pk} is {got!r}, expected {wanted!r}")


def _apply_assert_digest(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    """``machine``'s per-table digest matches every other declared machine
    that has synced -- same content, byte for byte, tombstones included.
    """
    requested = args.get("tables")
    if requested is None:
        tables: tuple[str, ...] = protocol.DIGEST_TABLES
    else:
        tables = tuple(str(t) for t in _require_nonempty_list(requested, where="assert_digest.tables"))
    against = args.get("against")
    if against is None:
        peers = [m.id for m in run.scenario.machines if m.id != machine]
    else:
        peers = [str(m) for m in _require_nonempty_list(against, where="assert_digest.against")]

    conn = run.conn(machine)
    try:
        mine = {table: protocol.table_digest(conn, table) for table in tables}
    finally:
        conn.close()
    for peer in peers:
        conn = run.conn(peer)
        try:
            theirs = {table: protocol.table_digest(conn, table) for table in tables}
        finally:
            conn.close()
        divergent = [table for table in tables if mine[table] != theirs[table]]
        if divergent:
            raise ScenarioError(
                f"{machine} and {peer} disagree on {divergent} (a real sync "
                f"would have raised SyncDigestMismatch here)"
            )


def _dispatch_step(run: SimRun, step: Step) -> None:
    """The action -> handler mapping, raising on anything unrecognized.

    Split out of :func:`_execute_step` (ruff TRY301): the dispatch is what
    can raise ``ScenarioError`` for an unknown action, and living in its own
    function keeps the outer ``try`` catching only calls, never raising
    directly itself.
    """
    if step.action == "edit":
        _ensure_spoke(run, step.on, "edit")
        _apply_edit(run, step.on, step.args)
    elif step.action == "sync":
        _ensure_spoke(run, step.on, "sync")
        _apply_sync(run, step.on, step.args)
    elif step.action == "assert_converged":
        _apply_assert_converged(run, step.on, step.args)
    elif step.action == "assert_digest":
        _apply_assert_digest(run, step.on, step.args)
    elif step.action == "partition":
        _ensure_spoke(run, step.on, "partition")
        _apply_partition(run, step.on, step.args)
    elif step.action == "reorder":
        _ensure_spoke(run, step.on, "reorder")
        _apply_reorder(run, step.on, step.args)
    elif step.action in _ADVERSARIAL_ACTIONS:
        dispatch_adversarial(run, step)
    else:
        raise ScenarioError(f"unknown action {step.action!r}")


def _execute_step(run: SimRun, index: int, step: Step) -> None:
    where = f"steps[{index}] ({step.action} on {step.on})"
    try:
        _dispatch_step(run, step)
    except ScenarioError as exc:
        raise ScenarioError(f"{where}: {exc}") from exc


def _assert_expected(run: SimRun) -> None:
    assert_faults_spent(run)
    expected = run.scenario.expected
    if "digest_converged" not in expected:
        return
    wanted = expected["digest_converged"]
    if not isinstance(wanted, bool):
        raise ScenarioError("expected.digest_converged must be true or false")
    hub_conn = run.conn(run.scenario.hub.id)
    try:
        hub_digest = protocol.sync_digest(hub_conn)
    finally:
        hub_conn.close()
    mismatched: list[str] = []
    for spoke in run.scenario.spokes:
        conn = run.conn(spoke.id)
        try:
            spoke_digest = protocol.sync_digest(conn)
        finally:
            conn.close()
        if spoke_digest.overall != hub_digest.overall:
            mismatched.append(spoke.id)
    converged = not mismatched
    if converged != wanted:
        raise ScenarioError(
            f"expected.digest_converged was {wanted} but observed convergence "
            f"was {converged} (machines out of step with the hub: {mismatched})"
        )


__all__ = ["_assert_expected", "_execute_step", "_seed"]
