"""Re-runnable fixture scenario harness for CLOUDSYNC (``specs/design_decision_07.md``
point 3: named, re-runnable scenarios that run in two modes with the SAME
assertions -- ``sim`` (this module, N data-dirs behind one in-process hub,
fast) and ``fleet`` (real machines, real daemons, wired below but not yet
executable).

A scenario is a YAML file under ``tests/cloudsync/scenarios/``:

    name: str
    description: str
    machines: [{id: str, role: hub|spoke}, ...]   # exactly one hub
    seed: {tracks: [...], playlists: [...], policies: [...]}   # all optional
    steps: [{on: <machine id>, action: edit|sync|assert_converged|
             assert_digest|partition, args: {...}}, ...]
    expected: {digest_converged: bool}             # optional, checked last

Sim mode never touches the network: the hub is the real ``apps.sync_hub``
FastAPI router behind a ``TestClient``, and every machine is a real migrated
state DB in a temp dir, driven through the real ``apps.sync_hub.client``
round trip -- the same non-mock pattern as
``tests/cloudsync/test_hub_sync.py``. Nothing here repairs a divergence: an
assertion step raises :class:`ScenarioError` and the run stops, same as
``client.run_sync`` raising :class:`~apps.sync_hub.client.SyncDigestMismatch`
mid-fleet.

Two writer identities appear in seeded/edited rows, deliberately different:

* Seed rows for ``tracks``/``playlists``/``playlist_memberships`` are stamped
  with the fixed origin :data:`_SEED_ORIGIN` on every machine that seeds them,
  so a byte-identical starting row never manufactures a spurious LWW tie
  between two random per-run machine ids.
* ``sync_policies``/``playlist_pins`` rows are always self-referential: a
  machine can only ever write its own ``machine_id`` (mirrors D5 -- "what
  THIS machine does with the policy is derived from its machine identity"),
  so both seeding and editing those two tables stamp ``machine_id`` and
  ``origin_device_id`` with the acting machine's own real id.
* ``edit`` steps on every table stamp the acting machine's own real id as
  ``origin_device_id``, exactly as a real writer would.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.shared.state import sync_stamp
from apps.sync_hub import client, protocol, service

SCENARIOS_DIR: Path = Path(__file__).resolve().parent / "scenarios"

# Real fleet member names (design_decision_07's D7 reference: agentbox is the
# hub, the rest are spokes). Fleet mode's --machine is restricted to this set
# regardless of what a scenario's own (sim-only, logical) machine ids are
# called -- a scenario written as ``spoke-a``/``spoke-b`` still names the real
# fleet member running it via this flag.
FLEET_MACHINE_IDS: tuple[str, ...] = ("silver", "air", "bifrost2", "agentbox")

_VALID_ROLES: frozenset[str] = frozenset({"hub", "spoke"})
_VALID_ACTIONS: frozenset[str] = frozenset(
    {"edit", "sync", "assert_converged", "assert_digest", "partition"}
)
_EDITABLE_TABLES: frozenset[str] = frozenset(
    {"tracks", "playlists", "sync_policies", "playlist_pins"}
)
_SYNC_RESULT_FIELDS: frozenset[str] = frozenset(
    {"pushed", "accepted", "rejected", "pulled", "applied"}
)

# Fixed origin for seed rows (never a real machine_id) so an identical
# starting row on several machines is byte-identical everywhere, not just
# value-identical -- no accidental LWW tie between two random per-run ids.
_SEED_ORIGIN: str = "seed-fixture"

# Sim mode's logical clock. Real wall-clock time would make LWW ordering
# depend on how fast the test runs; a fixed epoch plus one second per
# auto-stamped edit makes "the later step wins" a property of the YAML file,
# not of machine speed.
_TICK_EPOCH: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)


class ScenarioError(RuntimeError):
    """A scenario file, or a step within it, could not be executed."""


# ----- YAML schema -----------------------------------------------------------


@dataclass(frozen=True)
class MachineDecl:
    id: str
    role: str  # 'hub' | 'spoke'


@dataclass(frozen=True)
class Step:
    on: str
    action: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Scenario:
    """One parsed, validated scenario file."""

    name: str
    description: str
    machines: tuple[MachineDecl, ...]
    seed: dict[str, Any]
    steps: tuple[Step, ...]
    expected: dict[str, Any]
    source: Path

    @property
    def hub(self) -> MachineDecl:
        return next(machine for machine in self.machines if machine.role == "hub")

    @property
    def spokes(self) -> tuple[MachineDecl, ...]:
        return tuple(machine for machine in self.machines if machine.role == "spoke")


def _require(mapping: Mapping[str, Any], key: str, *, where: str) -> Any:
    if key not in mapping or mapping[key] is None:
        raise ScenarioError(f"{where}: missing required key {key!r}")
    return mapping[key]


def _require_mapping(value: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScenarioError(f"{where}: must be a mapping, got {type(value).__name__}")
    return value


def _require_nonempty_list(value: Any, *, where: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ScenarioError(f"{where}: must be a non-empty list")
    return value


def _parse_machines(raw: Any, *, where: str) -> tuple[MachineDecl, ...]:
    entries = _require_nonempty_list(raw, where=where)
    machines: list[MachineDecl] = []
    seen_ids: set[str] = set()
    for entry in entries:
        entry = _require_mapping(entry, where=f"{where}[]")
        machine_id = str(_require(entry, "id", where=f"{where}[]"))
        role = str(_require(entry, "role", where=f"{where}[{machine_id}]"))
        if role not in _VALID_ROLES:
            raise ScenarioError(
                f"{where}[{machine_id}].role must be one of {sorted(_VALID_ROLES)}, "
                f"got {role!r}"
            )
        if machine_id in seen_ids:
            raise ScenarioError(f"{where}: machine id {machine_id!r} declared twice")
        seen_ids.add(machine_id)
        machines.append(MachineDecl(id=machine_id, role=role))
    hub_ids = [m.id for m in machines if m.role == "hub"]
    if len(hub_ids) != 1:
        raise ScenarioError(
            f"{where}: exactly one machine must have role 'hub', found {hub_ids}"
        )
    if not any(m.role == "spoke" for m in machines):
        raise ScenarioError(f"{where}: at least one machine must have role 'spoke'")
    return tuple(machines)


def _parse_steps(raw: Any, *, where: str, machine_ids: frozenset[str]) -> tuple[Step, ...]:
    entries = _require_nonempty_list(raw, where=where)
    steps: list[Step] = []
    for index, entry in enumerate(entries):
        step_where = f"{where}[{index}]"
        entry = _require_mapping(entry, where=step_where)
        action = str(_require(entry, "action", where=step_where))
        if action not in _VALID_ACTIONS:
            raise ScenarioError(
                f"{step_where}.action must be one of {sorted(_VALID_ACTIONS)}, "
                f"got {action!r}"
            )
        on = str(_require(entry, "on", where=step_where))
        if on not in machine_ids:
            raise ScenarioError(
                f"{step_where}.on {on!r} is not a declared machine id "
                f"({sorted(machine_ids)})"
            )
        raw_args = entry.get("args", {})
        args = _require_mapping(raw_args, where=f"{step_where}.args") if raw_args else {}
        steps.append(Step(on=on, action=action, args=args))
    return tuple(steps)


def load_scenario(path: Path) -> Scenario:
    """Parse and validate one scenario file. Never returns a half-checked one."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    top = _require_mapping(raw, where=str(path))
    name = str(_require(top, "name", where=str(path)))
    description = str(_require(top, "description", where=str(path)))
    machines = _parse_machines(top.get("machines"), where=f"{path}: machines")
    seed = _require_mapping(top.get("seed") or {}, where=f"{path}: seed")
    machine_ids = frozenset(m.id for m in machines)
    steps = _parse_steps(top.get("steps"), where=f"{path}: steps", machine_ids=machine_ids)
    expected = _require_mapping(top.get("expected") or {}, where=f"{path}: expected")
    return Scenario(
        name=name,
        description=description,
        machines=machines,
        seed=seed,
        steps=steps,
        expected=expected,
        source=path,
    )


def discover_scenarios(directory: Path = SCENARIOS_DIR) -> list[Path]:
    """Every scenario YAML, sorted for deterministic test collection order."""
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise ScenarioError(f"no *.yaml scenarios found under {directory}")
    return paths


# ----- sim-mode transport (same non-mock pattern as test_hub_sync.py) ------


class _TestClientTransport:
    """A :class:`client.HubTransport` backed by a Starlette ``TestClient``.

    Not a mock of the hub: it drives the real router through the real ASGI
    stack. It exists only because ``TestClient`` is not a URL.
    """

    def __init__(self, http: TestClient) -> None:
        self._http = http

    def _decoded(self, response: Any, label: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise client.SyncTransportError(
                f"{label} -> HTTP {response.status_code}: {response.text}"
            )
        return dict(response.json())

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._decoded(self._http.post(path, json=dict(payload)), f"POST {path}")

    def get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        return self._decoded(self._http.get(path, params=dict(params)), f"GET {path}")


# ----- sim-mode run state ----------------------------------------------------


@dataclass
class SimRun:
    """Live state for one scenario execution in sim mode."""

    scenario: Scenario
    hub_transport: _TestClientTransport
    data_dirs: dict[str, Path]
    machine_ids: dict[str, str]
    offline: set[str] = field(default_factory=set)
    _tick: int = 0

    def conn(self, machine: str):  # -> sqlite3.Connection
        return state_db.open_rw(client.state_db_path(self.data_dirs[machine]))

    def next_tick(self) -> str:
        self._tick += 1
        return (_TICK_EPOCH + timedelta(seconds=self._tick)).isoformat()


def _stamp(args: Mapping[str, Any], run: SimRun) -> str:
    """The explicit ``updated_at`` an args block asked for, else the next tick."""
    explicit = args.get("updated_at")
    return str(explicit) if explicit is not None else run.next_tick()


def _log_edit(
    conn,  # sqlite3.Connection
    table: str,
    row_pk: tuple[Any, ...],
    origin: str,
    updated_at: str,
) -> str:
    """Append this edit to ``local_changelog`` and return its canonical stamp.

    An edit step is standing in for a real writer, and since ADR 08 point 3
    a real writer does not just set ``updated_at`` -- it logs the write to
    the spoke's own changelog, which is what ``sync_state.last_push_seq``
    fences against. A step that skipped this would edit a row that the next
    push could not see, which is a harness bug that would read exactly like
    a sync bug.
    """
    return sync_stamp.stamp_and_log(
        conn, table, row_pk, origin, now=updated_at
    ).updated_at


@contextmanager
def _sim_run(scenario: Scenario, tmp_path: Path) -> Iterator[SimRun]:
    hub_decl = scenario.hub
    hub_dir = tmp_path / hub_decl.id
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = hub_decl.id
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        data_dirs = {hub_decl.id: hub_dir}
        for spoke in scenario.spokes:
            data_dirs[spoke.id] = tmp_path / spoke.id
        run = SimRun(
            scenario=scenario,
            hub_transport=_TestClientTransport(http),
            data_dirs=data_dirs,
            machine_ids={},
        )
        # Register every machine's own identity up front: mints its
        # machine-id file and inserts its own `machines` row. Real daemons
        # do this on first boot; a self-referential sync_policies/
        # playlist_pins seed row needs that FK target to already exist, and
        # every edit step needs a real origin_device_id to stamp.
        for decl in scenario.machines:
            conn = run.conn(decl.id)
            try:
                identity = machine_identity.register_machine(
                    conn, data_dir=data_dirs[decl.id], name=decl.id
                )
                run.machine_ids[decl.id] = identity.machine_id
            finally:
                conn.close()
        yield run


def _ensure_spoke(run: SimRun, machine: str, action: str) -> None:
    if machine == run.scenario.hub.id:
        raise ScenarioError(
            f"{action} cannot target {machine!r}, the hub -- hub state is "
            f"derived entirely from spoke pushes, never edited/synced/"
            f"partitioned directly"
        )


def _validate_editable_columns(
    conn, table: str, changes: Mapping[str, Any]
) -> None:
    columns = set(protocol.table_columns(conn, table))
    spec = protocol.SPEC_BY_TABLE[table]
    forbidden = set(spec.pk) | set(protocol.SYNC_COLUMNS)
    for key in changes:
        if key not in columns:
            raise ScenarioError(f"{table} has no column {key!r}")
        if key in forbidden:
            raise ScenarioError(
                f"{table}.{key} is a primary-key or sync-trio column and "
                f"cannot be set directly"
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
                    stable_id, stable_id_tier, title, created_at, updated_at,
                    origin_device_id
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (stable_id, stable_id_tier, title, updated_at, updated_at, _SEED_ORIGIN),
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
            conn, "tracks", (stable_id,), origin, _stamp(args, run)
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
            conn, "playlists", (playlist_id,), origin, _stamp(args, run)
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
    updated_at = _stamp(args, run)
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
    else:
        _edit_policy(run, machine, table, args)


def _apply_sync(run: SimRun, machine: str, args: Mapping[str, Any]) -> None:
    if machine in run.offline:
        raise ScenarioError(
            f"machine is partitioned (offline); a real spoke cannot reach "
            f"the hub in this state -- add a 'partition' step to reconnect "
            f"it first"
        )
    result = client.run_sync(
        run.data_dirs[machine], "http://hub.invalid", transport=run.hub_transport, name=machine
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


def _resolve_pk(
    run: SimRun, table: str, raw_pk: Mapping[str, Any], *, self_machine: str
) -> dict[str, str]:
    """Expand a scenario's ``pk`` block to real column values.

    A string value starting with ``@`` resolves to that logical machine's
    real (randomly minted) ``machine_id`` -- the only way a YAML file can
    name another machine's row on a fleet-wide policy table without knowing
    its id in advance. ``sync_policies``/``playlist_pins`` default a missing
    ``machine_id`` to the checked machine's own id, since that is what a
    scenario asks for in the common case.
    """
    spec = protocol.SPEC_BY_TABLE.get(table)
    if spec is None and table == protocol.MEMBERSHIP_TABLE:
        spec = protocol.MEMBERSHIP_SPEC
    if spec is None:
        raise ScenarioError(f"{table!r} is not a syncable/digest table")
    resolved: dict[str, str] = {}
    for column, value in raw_pk.items():
        if isinstance(value, str) and value.startswith("@"):
            ref = value[1:]
            if ref not in run.machine_ids:
                raise ScenarioError(f"pk reference '@{ref}' is not a declared machine")
            resolved[column] = run.machine_ids[ref]
        else:
            resolved[column] = str(value)
    if table in ("sync_policies", "playlist_pins") and "machine_id" not in resolved:
        resolved["machine_id"] = run.machine_ids[self_machine]
    missing = [column for column in spec.pk if column not in resolved]
    if missing:
        raise ScenarioError(f"{table} pk incomplete: missing {missing}")
    return resolved


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


def _execute_step(run: SimRun, index: int, step: Step) -> None:
    where = f"steps[{index}] ({step.action} on {step.on})"
    try:
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
        else:
            raise ScenarioError(f"unknown action {step.action!r}")
    except ScenarioError as exc:
        raise ScenarioError(f"{where}: {exc}") from exc


def _assert_expected(run: SimRun) -> None:
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


# ----- entry points ------------------------------------------------------


def run_scenario_sim(scenario: Scenario, tmp_path: Path) -> None:
    """Execute one scenario end to end in sim mode. Raises on the first failure."""
    with _sim_run(scenario, tmp_path) as run:
        _seed(run)
        for index, step in enumerate(scenario.steps):
            _execute_step(run, index, step)
        _assert_expected(run)


def _find_scenario(name: str) -> Scenario:
    candidates: list[Scenario] = []
    for path in discover_scenarios():
        scenario = load_scenario(path)
        candidates.append(scenario)
        if scenario.name == name:
            return scenario
    raise ScenarioError(
        f"no scenario named {name!r} under {SCENARIOS_DIR} "
        f"(have: {[c.name for c in candidates]})"
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.cloudsync.scenario_runner",
        description=(
            "Run one CLOUDSYNC fixture scenario (specs/design_decision_07.md "
            "point 3)."
        ),
    )
    parser.add_argument(
        "--scenario", required=True, help="scenario name (the YAML's 'name' field)"
    )
    parser.add_argument("--mode", choices=("sim", "fleet"), default="sim")
    parser.add_argument(
        "--machine",
        choices=FLEET_MACHINE_IDS,
        default=None,
        help="fleet mode only: which real fleet machine this invocation runs as",
    )
    parser.add_argument(
        "--hub-url",
        default=None,
        help="fleet mode only: the hub daemon's URL (agentbox is the hub, D7)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="fleet mode only: this machine's real MDT_DATA_DIR",
    )
    return parser


def _run_fleet_cli(scenario: Scenario, args: argparse.Namespace) -> None:
    if args.machine is None:
        raise ScenarioError(
            f"--mode fleet needs --machine (one of: {', '.join(FLEET_MACHINE_IDS)})"
        )
    if args.hub_url is None:
        raise ScenarioError("--mode fleet needs --hub-url (the hub daemon's URL)")
    if args.data_dir is None:
        raise ScenarioError("--mode fleet needs --data-dir (this machine's real data root)")
    raise NotImplementedError(
        f"fleet mode is not implemented yet (specs/design_decision_07.md point "
        f"3). Running scenario {scenario.name!r} for real would need machine "
        f"{args.machine!r} (choices: {', '.join(FLEET_MACHINE_IDS)}; agentbox "
        f"is the hub per the D7 decision in specs/cloudsync-spec.md) running "
        f"the real daemon against its own --data-dir {args.data_dir}, "
        f"reachable at --hub-url {args.hub_url!r}, coordinated with the other "
        f"fleet members by hand or by an orchestrating agent. Sim mode "
        f"(--mode sim, the default) covers every assertion this scenario "
        f"makes today."
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        scenario = _find_scenario(args.scenario)
        if args.mode == "sim":
            with tempfile.TemporaryDirectory(prefix="cloudsync-scenario-") as tmp:
                run_scenario_sim(scenario, Path(tmp))
        elif args.mode == "fleet":
            _run_fleet_cli(scenario, args)
        else:
            raise ScenarioError(f"unknown --mode {args.mode!r}")
    except (ScenarioError, NotImplementedError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] {scenario.name} converged in {args.mode} mode")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "FLEET_MACHINE_IDS",
    "SCENARIOS_DIR",
    "MachineDecl",
    "Scenario",
    "ScenarioError",
    "Step",
    "discover_scenarios",
    "load_scenario",
    "main",
    "run_scenario_sim",
]
