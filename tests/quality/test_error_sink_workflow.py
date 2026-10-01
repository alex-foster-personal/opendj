"""OBS-01 Part 1: CI job failures post kind=build to the error sink.

Since ADR-0121 the sink runs as its own workflow, ci-error-sink.yml (hourly at :30,
daily reconcile at 03:23 UTC), instead of riding the retired cost guard's pass.

Regression lines:
  - if a failed CI/E2E/macOS Packaging run cannot reach the poster, then broken
  - if the sink watches a workflow name that no workflow has, then broken
  - if the poster does not invoke post_build_failure, then broken
  - if the workflow edits runner-switch variables, then broken

[if] a watched workflow fails [then] the batch pass posts it to the sink, [else stop].
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
ERROR_SINK = WORKFLOWS / "ci-error-sink.yml"

SINK_WORKFLOWS = {"CI", "E2E", "macOS Packaging"}
HOURLY_CRON = "30 * * * *"
RECONCILE_CRON = "23 3 * * *"


def _workflow() -> dict:
    document = yaml.safe_load(ERROR_SINK.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "ci-error-sink.yml is not a mapping"
    return document


def _sink_job() -> dict:
    return _workflow()["jobs"]["sink"]


def _names(csv: str) -> set[str]:
    return {name.strip() for name in csv.split(",") if name.strip()}


def _poster_step() -> dict:
    return next(
        step
        for step in _sink_job()["steps"]
        if isinstance(step, dict) and "scripts.post_build_failure" in step.get("run", "")
    )


def _workflow_names() -> set[str]:
    return {
        str(yaml.safe_load(path.read_text(encoding="utf-8"))["name"])
        for path in WORKFLOWS.glob("*.y*ml")
    }


def test_the_per_completion_sink_workflow_is_gone() -> None:
    assert not (WORKFLOWS / "error-sink.yml").exists(), (
        "the sink rides a scheduled batch pass; a workflow_run follower per completion "
        "is the job this round removed"
    )


def test_a_failed_watched_workflow_reaches_the_kind_build_poster() -> None:
    """if CI, E2E or macOS Packaging fails then the batch pass posts it."""
    env = _sink_job()["env"]
    assert _names(env["LISTED_WORKFLOWS"]) == SINK_WORKFLOWS
    batch = next(step for step in _sink_job()["steps"] if step.get("id") == "sink")
    assert "scripts.ci_error_sink_batch" in batch["run"]
    assert '--listed "$LISTED_WORKFLOWS"' in batch["run"]
    poster = _poster_step()
    assert "--batch-file" in poster["run"]
    assert poster["env"]["SINK_FAILURES_FILE"] == "${{ steps.sink.outputs.sink_failures_file }}"
    body = ERROR_SINK.read_text(encoding="utf-8")
    assert "CI_RUNS_ON_LINUX" in body
    assert "gh variable" not in body


def test_every_sink_workflow_is_a_real_workflow_name() -> None:
    missing = SINK_WORKFLOWS - _workflow_names()
    assert not missing, f"no workflow is named {sorted(missing)}"


def test_the_sink_job_runs_on_an_hourly_cadence_and_daily_reconcile() -> None:
    # PyYAML reads the bare key `on:` as the boolean True (YAML 1.1).
    triggers = _workflow()[True]
    crons = [entry["cron"] for entry in triggers["schedule"]]
    assert HOURLY_CRON in crons
    assert RECONCILE_CRON in crons
    assert "if" not in _sink_job(), "every run of this file is a sink pass, so no job is conditional"


def test_ci_failure_poster_uses_canonical_host_not_runner_name() -> None:
    body = ERROR_SINK.read_text(encoding="utf-8")
    assert "${{ runner.name }}" not in body
    poster = _poster_step()
    assert '--host "github-actions"' in poster["run"]
    assert poster["env"]["OPENDJ_HOST_LABEL"] == "github-actions"
    assert poster["env"]["OPENDJ_TELEMETRY"] == "0"
    assert re.search(r"mkdir -p \"\$HOME/jobs/logs\"", poster["run"]), (
        "sink_path() picks the nucbox JSONL only when its directory exists"
    )
