"""Bookkeeping workflows stay pinned to the nucbox runner (#11341).

Regression lines:
  - if Stable evidence moves off CI_RUNS_ON_UNPRIVILEGED_LINUX then broken
  - if CI error sink moves off CI_RUNS_ON_UNPRIVILEGED_LINUX then broken
  - if bookkeeping cancel-in-progress flips to true then broken
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = {
    "Stable evidence": (REPO / ".github" / "workflows" / "stable-evidence.yml", "append"),
    "CI error sink": (REPO / ".github" / "workflows" / "ci-error-sink.yml", "sink"),
}
RUNS_ON_EXPR = "${{ fromJSON(vars.CI_RUNS_ON_UNPRIVILEGED_LINUX) }}"


def _workflow(path: Path) -> dict:
    assert path.is_file(), f"workflow is missing: {path}"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path.name} is not a mapping"
    return document


def test_bookkeeping_workflows_pin_the_nucbox_runner() -> None:
    for name, (path, job_name) in WORKFLOWS.items():
        workflow = _workflow(path)
        job = workflow["jobs"][job_name]
        assert job["runs-on"] == RUNS_ON_EXPR, f"{name} must stay on CI_RUNS_ON_UNPRIVILEGED_LINUX (nucbox), with no hosted fallback"


def test_bookkeeping_workflows_keep_cancel_in_progress_false() -> None:
    for name, (path, job_name) in WORKFLOWS.items():
        workflow = _workflow(path)
        job = workflow["jobs"][job_name]
        concurrency = job.get("concurrency") or workflow.get("concurrency") or {}
        assert concurrency.get("cancel-in-progress") is False, f"{name} must keep cancel-in-progress: false"
