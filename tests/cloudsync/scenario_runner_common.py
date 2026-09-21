"""Scenario schema, sim-mode transport and run state -- the shared base.

Split out of :mod:`tests.cloudsync.scenario_runner` (quality-gate file_size
ratchet, round 4): both the seeding/step-execution module
(:mod:`tests.cloudsync.scenario_runner_actions`) and the harness entry
points (still in ``scenario_runner``) need ``ScenarioError``, the parsed
``Scenario``/``Step`` types, ``SimRun`` and the small write-path validators
below. Living here, with no import of either of those two modules, is what
keeps the three-way split acyclic -- ``scenario_runner`` runs as ``__main__``
under ``python -m``, which re-imports itself under its real dotted name the
moment anything does a relative import back into it, and a module executing
twice with a real circular import between two of its own submodules resolves
one of them partially initialized. Neither ``scenario_runner`` nor
``scenario_runner_actions`` imports the other; both import only this module.

See ``scenario_runner``'s own docstring for the YAML scenario format and the
sim/fleet mode contract this schema and run state serve.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity, sync_stamp
from apps.sync_hub import client, protocol, service

from .fault_transport import FaultTransport

SCENARIOS_DIR: Path = Path(__file__).resolve().parent / "scenarios"

# Real fleet member names (design_decision_07's D7 reference: agentbox is the
# hub, the rest are spokes). Fleet mode's --machine is restricted to this set
# regardless of what a scenario's own (sim-only, logical) machine ids are
# called -- a scenario written as ``spoke-a``/``spoke-b`` still names the real
# fleet member running it via this flag.
FLEET_MACHINE_IDS: tuple[str, ...] = ("silver", "air", "bifrost2", "agentbox")

_VALID_ROLES: frozenset[str] = frozenset({"hub", "spoke"})
# The adversarial verbs (plan W9), dispatched by
# :mod:`tests.cloudsync.scenario_runner_adversarial`. Declared here, not
# there, so the parser validates them without importing that module.
_ADVERSARIAL_ACTIONS: frozenset[str] = frozenset(
    {"delete", "reinsert", "skew_clock", "restore_hub", "prune", "fault", "rename_machine"}
)
_VALID_ACTIONS: frozenset[str] = frozenset(
    {"edit", "sync", "assert_converged", "assert_digest", "partition", "reorder"}
) | _ADVERSARIAL_ACTIONS
_EDITABLE_TABLES: frozenset[str] = frozenset(
    {
        "tracks",
        "playlists",
        "sync_policies",
        "playlist_pins",
        "track_fields",
        "track_locations",
        "playlist_memberships",
    }
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
_TICK_EPOCH: datetime = datetime(2026, 1, 1, tzinfo=UTC)


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
    """Live state for one scenario execution in sim mode.

    ``transports`` holds one :class:`FaultTransport` per spoke, created on
    first use around the shared hub transport, so a ``fault`` step armed on
    one spoke never fires on another. ``clock_skew_s`` offsets a machine's
    auto-stamped edits (``skew_clock``), ``names`` is the name a spoke syncs
    under (``rename_machine``), and ``hub_snapshots`` holds the hub DB copies
    ``restore_hub`` took.
    """

    scenario: Scenario
    hub_transport: _TestClientTransport
    data_dirs: dict[str, Path]
    machine_ids: dict[str, str]
    offline: set[str] = field(default_factory=set)
    transports: dict[str, FaultTransport] = field(default_factory=dict)
    clock_skew_s: dict[str, float] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    hub_snapshots: dict[str, Path] = field(default_factory=dict)
    _tick: int = 0

    def conn(self, machine: str):  # -> sqlite3.Connection
        return state_db.open_rw(client.state_db_path(self.data_dirs[machine]))

    def next_tick(self, machine: str | None = None) -> str:
        """The next logical instant, shifted by ``machine``'s clock skew if any."""
        self._tick += 1
        skew = self.clock_skew_s.get(machine, 0.0) if machine is not None else 0.0
        return (_TICK_EPOCH + timedelta(seconds=self._tick + skew)).isoformat()

    def transport_for(self, machine: str) -> FaultTransport:
        if machine not in self.transports:
            self.transports[machine] = FaultTransport(self.hub_transport)
        return self.transports[machine]

    def sync_name(self, machine: str) -> str:
        return self.names.get(machine, machine)


def _stamp(args: Mapping[str, Any], run: SimRun, machine: str) -> str:
    """The explicit ``updated_at`` an args block asked for, else ``machine``'s next tick."""
    explicit = args.get("updated_at")
    return str(explicit) if explicit is not None else run.next_tick(machine)


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


def _resolve_pk(
    run: SimRun, table: str, raw_pk: Mapping[str, Any], *, self_machine: str
) -> dict[str, str]:
    """Expand a scenario's ``pk`` block to real column values.

    A string value starting with ``@`` resolves to that logical machine's
    real (randomly minted) ``machine_id`` -- the only way a YAML file can
    name another machine's row on a fleet-wide policy table without knowing
    its id in advance. ``sync_policies``/``playlist_pins`` default a missing
    ``machine_id`` to the checked machine's own id, since that is what a
    scenario asks for in the common case. ``machines`` (the registry) is
    addressable too, so a ``rename_machine`` step can be asserted.
    """
    spec = protocol.SPEC_BY_TABLE.get(table)
    if spec is None and table == protocol.MEMBERSHIP_TABLE:
        spec = protocol.MEMBERSHIP_SPEC
    if spec is None and table == protocol.REGISTRY_TABLE:
        spec = protocol.TableSpec(protocol.REGISTRY_TABLE, ("machine_id",))
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


__all__ = [
    "FLEET_MACHINE_IDS",
    "SCENARIOS_DIR",
    "MachineDecl",
    "Scenario",
    "ScenarioError",
    "SimRun",
    "Step",
    "discover_scenarios",
    "load_scenario",
]
