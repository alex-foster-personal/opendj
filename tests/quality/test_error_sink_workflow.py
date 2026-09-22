"""OBS-01 Part 1: CI job failures post kind=build to the error sink.

Reads the shipped workflow, not a second hand-maintained representation.

Regression lines:
  - if a failed CI/E2E/Full CI/macOS packaging run cannot reach the poster,
    then broken
  - if the poster does not invoke post_build_failure, then broken
  - if the workflow edits runner-switch variables, then broken
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "error-sink.yml"

WATCHED = ["CI", "E2E", "Full CI", "macOS packaging"]


def _workflow() -> dict:
    assert WORKFLOW.is_file(), (
        "the error-sink workflow is missing, so a CI job failure has no "
        "kind=build event on the one sink"
    )
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "error-sink.yml is not a mapping"
    return document


def _assert_watched(workflow: dict) -> None:
    trigger = workflow[True]["workflow_run"]
    assert trigger["workflows"] == WATCHED


def test_a_failed_watched_workflow_reaches_the_kind_build_poster() -> None:
    """if CI, E2E, Full CI, or macOS packaging fails then the sink job runs."""
    workflow = _workflow()
    _assert_watched(workflow)
    trigger = workflow[True]["workflow_run"]
    assert trigger["types"] == ["completed"]

    job = workflow["jobs"]["post_build_failure"]
    condition = " ".join(str(job["if"]).split())
    assert "github.event.workflow_run.conclusion == 'failure'" in condition
    command = "\n".join(
        step.get("run", "") for step in job["steps"] if isinstance(step, dict)
    )
    assert "scripts.post_build_failure" in command
    assert "--kind" in command and "build" in command
    body = WORKFLOW.read_text(encoding="utf-8")
    assert "gh variable" not in body
    assert "CI_RUNS_ON_LINUX" in body
    assert "gh variable set" not in body


def test_ci_failure_poster_uses_canonical_host_not_runner_name() -> None:
    """The posted host is always the canonical label, whether the poster
    interpolates it literally or through a step env var (zizmor: workflow_run
    fields must reach the shell through env, never inline)."""
    workflow = _workflow()
    body = WORKFLOW.read_text(encoding="utf-8")
    assert "${{ runner.name }}" not in body
    job = workflow["jobs"]["post_build_failure"]
    poster_step = next(
        step
        for step in job["steps"]
        if isinstance(step, dict) and "scripts.post_build_failure" in step.get("run", "")
    )
    command = poster_step["run"]
    if '--host "github-actions"' in command:
        return
    match = re.search(r'--host "\$\{(\w+)\}"', command)
    assert match, "--host must be either the literal canonical label or a step env var"
    assert poster_step.get("env", {}).get(match.group(1)) == "github-actions", (
        "the host env var must resolve to the canonical 'github-actions' label, "
        "not a runner-derived value"
    )


def test_dropping_macos_packaging_from_the_watch_list_fails() -> None:
    """A headless dmg / packaging failure cannot silently leave the watch list."""
    mutated = deepcopy(_workflow())
    mutated[True]["workflow_run"]["workflows"] = ["CI", "E2E", "Full CI"]
    try:
        _assert_watched(mutated)
    except AssertionError:
        return
    raise AssertionError("dropping macOS packaging from the watch list must fail")
