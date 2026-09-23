"""Quality ratchet for the trunk-tip-only sweeper workflow.

Regression lines:
  - if trunk-tip-only binds to the general agentbox pool then broken
  - if trunk-tip-only depends on GitHub-hosted runners alone then broken
  - if the sweeper lacks actions: write then broken
  - if the sweeper does not invoke scripts.ci_trunk_tip_only then broken
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "trunk-tip-only.yml"


def _workflow() -> dict:
    assert WORKFLOW.is_file(), "trunk-tip-only workflow is missing"
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "trunk-tip-only workflow is not a mapping"
    return document


def test_trunk_tip_only_runs_on_the_main_fix_reserve_not_agentbox() -> None:
    """[if] the sweeper can be queued behind the general agentbox backlog it exists to
    trim, or can only ever run on GitHub-hosted runners [then] broken, [else stop]."""
    workflow = _workflow()
    job = workflow["jobs"]["sweep"]
    runs_on = str(job["runs-on"])
    assert runs_on == "${{ fromJSON(vars.CI_RUNS_ON_MAIN_FIX || '\"ubuntu-latest\"') }}", (
        "trunk-tip-only must take the ADR-0041 main-fix reserve with a hosted fallback; "
        f"got {runs_on!r}"
    )
    for general_pool in ("CI_RUNS_ON_LINUX", "CI_RUNS_ON_E2E", "CI_RUNS_ON_PYTEST"):
        assert general_pool not in runs_on, f"{general_pool} is the backlog, not the reserve"


def test_trunk_tip_only_has_actions_write_and_main_triggers() -> None:
    """The sweeper can cancel runs and fires on main push plus schedule."""
    workflow = _workflow()
    permissions = workflow.get("permissions") or {}
    assert permissions.get("actions") == "write"

    on = workflow[True]
    assert "push" in on
    assert on["push"]["branches"] == ["main"]
    assert "schedule" in on
    assert "workflow_dispatch" in on


def test_trunk_tip_only_invokes_sweep_script() -> None:
    """The workflow runs scripts.ci_trunk_tip_only."""
    workflow = _workflow()
    steps = workflow["jobs"]["sweep"]["steps"]
    command = "\n".join(step.get("run", "") for step in steps)
    assert "python -m scripts.ci_trunk_tip_only" in command
