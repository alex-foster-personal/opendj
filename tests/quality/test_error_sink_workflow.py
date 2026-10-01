"""OBS-01 Part 1: CI job failures post kind=build to the error sink.

Since RUN-COUNT round 3 the sink rides the cost guard's scheduled pass instead of a
workflow_run job per completion. Reads the shipped workflow, not a second
hand-maintained representation.

Regression lines:
  - if a failed CI/E2E/macOS Packaging run cannot reach the poster, then broken
  - if the sink watches a workflow name that no workflow has, then broken
  - if the poster does not invoke post_build_failure, then broken
  - if the workflow edits runner-switch variables, then broken

[if] a watched workflow fails [then] the guard's pass posts it to the sink, [else stop].
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
GUARD = WORKFLOWS / "ci-cost-guard.yml"

SINK_WORKFLOWS = {"CI", "E2E", "macOS Packaging"}


def _guard() -> dict:
    document = yaml.safe_load(GUARD.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "ci-cost-guard.yml is not a mapping"
    return document


def _job() -> dict:
    return _guard()["jobs"]["assess"]


def _names(csv: str) -> set[str]:
    return {name.strip() for name in csv.split(",") if name.strip()}


def _poster_step() -> dict:
    return next(
        step
        for step in _job()["steps"]
        if isinstance(step, dict) and "scripts.post_build_failure" in step.get("run", "")
    )


def _workflow_names() -> set[str]:
    return {
        str(yaml.safe_load(path.read_text(encoding="utf-8"))["name"])
        for path in WORKFLOWS.glob("*.y*ml")
    }


def test_the_per_completion_sink_workflow_is_gone() -> None:
    assert not (WORKFLOWS / "error-sink.yml").exists(), (
        "the sink rides the cost guard's pass; a workflow_run follower per completion "
        "is the job this round removed"
    )


def test_a_failed_watched_workflow_reaches_the_kind_build_poster() -> None:
    """if CI, E2E or macOS Packaging fails then the guard's pass posts it."""
    env = _job()["env"]
    assert _names(env["SINK_WORKFLOWS"]) == SINK_WORKFLOWS
    assert _names(env["SINK_WORKFLOWS"]) <= _names(env["WATCHED_WORKFLOWS"]), (
        "the sink selects from the pass's own listing, so it can only see watched runs"
    )
    guard = next(step for step in _job()["steps"] if step.get("id") == "guard")
    assert '--sink-workflows "$SINK_WORKFLOWS"' in guard["run"]
    poster = _poster_step()
    assert "--batch-file" in poster["run"]
    assert poster["env"]["SINK_FAILURES_FILE"] == "${{ steps.guard.outputs.sink_failures_file }}"
    body = GUARD.read_text(encoding="utf-8")
    assert "CI_RUNS_ON_LINUX" in body
    assert "gh variable" not in body


def test_every_sink_workflow_is_a_real_workflow_name() -> None:
    """The old trigger watched `Full CI` and `macOS packaging`, and neither matched a
    workflow, so those failures were never posted. Held against the names on disk."""
    missing = SINK_WORKFLOWS - _workflow_names()
    assert not missing, f"no workflow is named {sorted(missing)}"


def test_the_sink_step_runs_after_the_alerts_and_before_the_census_gate() -> None:
    """A sink failure must never delay an alert, and the census gate stays last."""
    names = [step.get("name", "") for step in _job()["steps"]]
    poster = names.index(_poster_step()["name"])
    assert names.index("Open cost alert issues") < poster
    assert poster == len(names) - 2
    assert names[-1] == "Fail while the census holds the mark"


def test_ci_failure_poster_uses_canonical_host_not_runner_name() -> None:
    """The posted host is always the canonical label, never a runner-derived value."""
    body = GUARD.read_text(encoding="utf-8")
    assert "${{ runner.name }}" not in body
    poster = _poster_step()
    assert '--host "github-actions"' in poster["run"]
    assert poster["env"]["OPENDJ_HOST_LABEL"] == "github-actions"
    assert poster["env"]["OPENDJ_TELEMETRY"] == "0"
    assert re.search(r"mkdir -p \"\$HOME/jobs/logs\"", poster["run"]), (
        "sink_path() picks the nucbox JSONL only when its directory exists"
    )
