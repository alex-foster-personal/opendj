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
  with a fixed seed origin on every machine that seeds them, so a
  byte-identical starting row never manufactures a spurious LWW tie between
  two random per-run machine ids.
* ``sync_policies``/``playlist_pins`` rows are always self-referential: a
  machine can only ever write its own ``machine_id`` (mirrors D5 -- "what
  THIS machine does with the policy is derived from its machine identity"),
  so both seeding and editing those two tables stamp ``machine_id`` and
  ``origin_device_id`` with the acting machine's own real id.
* ``edit`` steps on every table stamp the acting machine's own real id as
  ``origin_device_id``, exactly as a real writer would.

The schema, sim-mode transport and run state
(:mod:`tests.cloudsync.scenario_runner_common`) and the seeding/step
execution (:mod:`tests.cloudsync.scenario_runner_actions`) split out
(quality-gate file_size ratchet, round 4); this module is the entry points
below, re-exporting the common module's public names so every caller that
already does ``from tests.cloudsync import scenario_runner`` and reads
``scenario_runner.X`` keeps the surface it had.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .scenario_runner_actions import _assert_expected, _execute_step, _seed
from .scenario_runner_common import (
    FLEET_MACHINE_IDS,
    SCENARIOS_DIR,
    MachineDecl,
    Scenario,
    ScenarioError,
    Step,
    _sim_run,
    discover_scenarios,
    load_scenario,
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


def _run_mode(scenario: Scenario, args: argparse.Namespace) -> None:
    """The ``--mode`` dispatch, raising on anything unrecognized.

    Split out of :func:`main` (ruff TRY301) for the same reason
    :func:`~tests.cloudsync.scenario_runner_actions._dispatch_step` was: the
    outer ``try`` should catch, not raise.
    """
    if args.mode == "sim":
        with tempfile.TemporaryDirectory(prefix="cloudsync-scenario-") as tmp:
            run_scenario_sim(scenario, Path(tmp))
    elif args.mode == "fleet":
        _run_fleet_cli(scenario, args)
    else:
        raise ScenarioError(f"unknown --mode {args.mode!r}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        scenario = _find_scenario(args.scenario)
        _run_mode(scenario, args)
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
