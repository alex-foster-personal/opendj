"""Quality ratchet for the main e2e starvation watchdog (issue #5831, W46).

Regression lines:
  - if the watchdog binds to the general e2e pool then broken
  - if the watchdog lacks actions: write and issues: write then broken
  - if the watchdog does not invoke scripts.ci_e2e_main_starvation then broken
  - if the watchdog wakes on E2E `workflow_run` again then it starts about 1,350 runs a day on
    the main-fix reserve for an alarm at most one cron gap earlier
  - if the ten-minute cron goes then nothing wakes the watchdog
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


def test_e2e_main_starvation_has_write_permissions_and_wakes_on_its_cron_only() -> None:
    workflow = _workflow()
    permissions = workflow.get("permissions") or {}
    assert permissions.get("actions") == "write"
    assert permissions.get("issues") == "write"

    on = workflow[True]
    # The cron alone (ADR-NEW-starvation-watchdog-cron-only): it delivered 134 of 144 slots on
    # Fri 6 Nov (max gap 33.4 min) while the E2E workflow_run wake started about 1,350 sweeps a
    # day, one a minute against a 30-minute threshold, on the main-fix reserve.
    assert set(on) == {"schedule", "workflow_dispatch"}
    assert on["schedule"] == [{"cron": "3,13,23,33,43,53 * * * *"}]


def test_e2e_main_starvation_invokes_watch_script() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["watch"]["steps"]
    command = "\n".join(step.get("run", "") for step in steps)
    assert "-m scripts.ci_e2e_main_starvation" in command
