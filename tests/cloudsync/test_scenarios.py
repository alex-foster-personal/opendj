"""Fast-lane pytest wrapper over every scenario in ``tests/cloudsync/scenarios/``
(``specs/design_decision_07.md`` point 3).

Each YAML file becomes one parametrized test id, run in sim mode -- N data
dirs behind one in-process hub, the same non-mock pattern as
``test_hub_sync.py``. Adding a new ``.yaml`` file under ``scenarios/`` picks
it up automatically; nothing here needs editing.

Acceptance criteria:
- if a new scenario file under ``scenarios/`` is not collected as its own
  test, the "re-runnable fixture scenario" contract from ADR 07 is broken.
- if a scenario's own ``assert_converged``/``assert_digest`` steps and its
  ``expected.digest_converged`` block disagree, the harness let a
  convergence bug through -- broken.
- if a malformed ``machines:`` block (zero or two 'hub' roles) loads without
  error, a scenario author's typo would silently run against the wrong
  topology -- broken.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.cloudsync.scenario_runner import (
    Scenario,
    discover_scenarios,
    load_scenario,
    run_scenario_sim,
)

pytestmark = pytest.mark.requirement("CAT-04")

_SCENARIO_PATHS: list[Path] = discover_scenarios()


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer sim registration."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.mark.parametrize(
    "scenario_path", _SCENARIO_PATHS, ids=[path.stem for path in _SCENARIO_PATHS]
)
def test_scenario_converges_in_sim_mode(scenario_path: Path, tmp_path: Path) -> None:
    """Load one scenario and run it to completion in sim mode."""
    scenario = load_scenario(scenario_path)
    run_scenario_sim(scenario, tmp_path)


def test_every_scenario_declares_exactly_one_hub_and_a_spoke() -> None:
    """Tripwire: a malformed ``machines:`` block fails at load, not mid-run."""
    for path in _SCENARIO_PATHS:
        scenario: Scenario = load_scenario(path)
        assert scenario.hub.role == "hub", scenario.name
        assert len(scenario.spokes) >= 1, scenario.name
        assert scenario.hub.id not in {spoke.id for spoke in scenario.spokes}, scenario.name


def test_every_scenario_declares_at_least_one_step_and_a_description() -> None:
    """A scenario with no steps or no description documents nothing."""
    for path in _SCENARIO_PATHS:
        scenario = load_scenario(path)
        assert scenario.steps, scenario.name
        assert scenario.description.strip(), scenario.name
