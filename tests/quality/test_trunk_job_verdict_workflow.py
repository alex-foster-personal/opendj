"""Issue #1309: the per-job trunk verdict must inspect CI and E2E runs.

Regression lines:
  - if E2E is absent from the watched workflows then a red trunk E2E is unwatched
  - if the verdict runs outside the default branch then non-trunk failures can page
  - if a green watched run records a buried failure then the watchdog is always-on
  - if the verdict job stops skipping a cancelled triggering run then a
    cancelled trunk run pages as if it had a verdict (#2642)
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "trunk-job-verdict.yml"


def test_trunk_job_verdict_watches_every_trunk_check_workflow() -> None:
    """A planted E2E failure reaches the independent verdict classifier."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow[True]["workflow_run"]["workflows"] == ["CI", "E2E"]

    job = workflow["jobs"]["verdict"]
    # Pin the invariants the condition must uphold, not the exact expression
    # string: trunk-only AND (since #2642) not-cancelled. A literal string
    # pin re-breaks on every future clause added to this `if` for an
    # unrelated reason.
    condition = " ".join(str(job["if"]).split())
    assert (
        "github.event.workflow_run.head_branch == github.event.repository.default_branch"
        in condition
    ), "verdict job must stay scoped to the default branch"
    assert "github.event.workflow_run.conclusion != 'cancelled'" in condition, (
        "verdict job must skip a cancelled triggering run (#2642)"
    )
    record = next(
        step
        for step in job["steps"]
        if step.get("name") == "Record the buried failure and fail visibly"
    )
    assert "steps.verdict.outputs.buried_failures != '0'" in record["if"]
