"""Quality ratchet for the main e2e starvation watchdog (issue #5831, W46).

Regression lines:
  - if the watchdog binds to the general e2e pool then broken
  - if the watchdog lacks actions: write and issues: write then broken
  - if the watchdog does not invoke scripts.ci_e2e_main_starvation then broken
  - if the watchdog wakes on E2E `requested` as well as `completed` then it starts twice the
    runs on the main-fix reserve for no earlier alarm
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "e2e-main-starvation.yml"


def _workflow() -> dict:
    assert WORKFLOW.is_file(), "e2e-main-starvation workflow is missing"
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "e2e-main-starvation workflow is not a mapping"
    return document


def test_e2e_main_starvation_runs_on_the_main_fix_reserve() -> None:
    """[if] the watchdog queues behind the e2e backlog it watches [then] broken, [else stop]."""
    workflow = _workflow()
    job = workflow["jobs"]["watch"]
    runs_on = str(job["runs-on"])
    assert runs_on == "${{ fromJSON(vars.CI_RUNS_ON_MAIN_FIX || '\"ubuntu-latest\"') }}", (
        "e2e-main-starvation must take the ADR-0041 main-fix reserve with a hosted fallback; "
        f"got {runs_on!r}"
    )
    for general_pool in ("CI_RUNS_ON_E2E", "CI_RUNS_ON_LINUX"):
        assert general_pool not in runs_on, f"{general_pool} is the backlog, not the reserve"


def test_e2e_main_starvation_has_write_permissions_and_e2e_triggers() -> None:
    workflow = _workflow()
    permissions = workflow.get("permissions") or {}
    assert permissions.get("actions") == "write"
    assert permissions.get("issues") == "write"

    on = workflow[True]
    assert "workflow_run" in on
    assert on["workflow_run"]["workflows"] == ["E2E"]
    # One wake per E2E run, not two (ADR-NEW-starvation-watchdog-wakes-on-completed): of its
    # 1,177 runs in 24 h from Thu 5 Nov 03:00Z only 53 came from the cron; the rest were E2E
    # workflow_run sweeps, about one a minute against a 30-minute threshold, on the main-fix reserve.
    assert on["workflow_run"]["types"] == ["completed"]
    assert "schedule" in on
    assert "workflow_dispatch" in on


def test_e2e_main_starvation_invokes_watch_script() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["watch"]["steps"]
    command = "\n".join(step.get("run", "") for step in steps)
    assert "-m scripts.ci_e2e_main_starvation" in command
