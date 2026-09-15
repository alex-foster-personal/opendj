"""Bookkeeping workflow_run jobs skip cancelled triggering runs.

Reads the shipped workflows, not a second hand-maintained representation.

Regression lines:
  - if stable-evidence append runs on a cancelled CI/E2E run, then broken
  - if trunk-job-verdict verdict runs on a cancelled trunk run, then broken
  - if ci-cost-guard assess stops pricing cancelled runs, then broken
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
STABLE_EVIDENCE = REPO / ".github" / "workflows" / "stable-evidence.yml"
TRUNK_JOB_VERDICT = REPO / ".github" / "workflows" / "trunk-job-verdict.yml"
CI_COST_GUARD = REPO / ".github" / "workflows" / "ci-cost-guard.yml"

CANCELLED_SKIP = "github.event.workflow_run.conclusion != 'cancelled'"


def _workflow(path: Path) -> dict:
    assert path.is_file(), f"the workflow is missing: {path}"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path.name} is not a mapping"
    return document


def _job_if(workflow: dict, job_name: str) -> str:
    job = workflow["jobs"][job_name]
    assert "if" in job, f"{job_name} job has no if field"
    return " ".join(str(job["if"]).split())


def test_stable_evidence_append_skips_cancelled_triggering_run() -> None:
    """if CI or E2E is cancelled then stable-evidence append does not allocate a runner."""
    workflow = _workflow(STABLE_EVIDENCE)
    trigger = workflow[True]["workflow_run"]
    assert trigger["types"] == ["completed"]

    condition = _job_if(workflow, "append")
    assert CANCELLED_SKIP in condition


def test_trunk_job_verdict_skips_cancelled_triggering_run() -> None:
    """if a trunk CI or E2E run is cancelled then the verdict job does not allocate a runner."""
    workflow = _workflow(TRUNK_JOB_VERDICT)
    trigger = workflow[True]["workflow_run"]
    assert trigger["types"] == ["completed"]

    condition = _job_if(workflow, "verdict")
    assert "github.event.repository.default_branch" in condition
    assert CANCELLED_SKIP in condition


def test_ci_cost_guard_still_prices_cancelled_runs() -> None:
    """ci-cost-guard assess must keep pricing cancelled runs on purpose."""
    workflow = _workflow(CI_COST_GUARD)
    job = workflow["jobs"]["assess"]
    if_field = job.get("if")
    if if_field is not None:
        condition = " ".join(str(if_field).split())
        assert CANCELLED_SKIP not in condition
