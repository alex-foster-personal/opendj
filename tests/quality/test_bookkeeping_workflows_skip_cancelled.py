"""Bookkeeping workflow_run jobs skip cancelled triggering runs.

Reads the shipped workflows, not a second hand-maintained representation.

Regression lines:
  - if stable-evidence's batch pass filters on nothing but status, reads its mark from
    a cache, or can fail to overlap the previous pass, then broken
  - if trunk-job-verdict verdict runs on a cancelled trunk run, then broken
  - if the error-sink batch pass reads its mark from a cache, then broken
  - if two consecutive passes can fail to overlap, then broken
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from scripts.stable_evidence_batch import RECORDED_WORKFLOWS

REPO = Path(__file__).resolve().parents[2]
STABLE_EVIDENCE = REPO / ".github" / "workflows" / "stable-evidence.yml"
TRUNK_JOB_VERDICT = REPO / ".github" / "workflows" / "trunk-job-verdict.yml"
BUDGET_WATCH = REPO / ".github" / "workflows" / "ci-budget-watch.yml"

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


def _cadence_minutes(cron: str) -> int:
    match = re.fullmatch(r"(\d+) \* \* \* \*", cron)
    if match:
        return 60
    match = re.fullmatch(r"(?:\*|\d+-59)/(\d+) \* \* \* \*", cron)
    assert match, f"not a supported schedule for overlap checks: {cron}"
    return int(match.group(1))


def _assert_batch_pass(
    workflow: dict,
    job_name: str,
    workflow_file: str,
    lookback_floor_min: int,
    listing_step_id: str,
    schedule_cron: str,
    reconcile_cron: str,
    *,
    expect_census: bool,
) -> None:
    job = workflow["jobs"][job_name]
    steps = job["steps"]
    mark = next(step for step in steps if step.get("id") == "mark")
    assert "scripts.ci_run_batch mark" in mark["run"], "the mark comes from the tested reader"
    assert f"--workflow-file {workflow_file}" in mark["run"]
    assert "per_page" not in mark["run"] and "failure" not in mark["run"]
    listing = next(step for step in steps if step.get("id") == listing_step_id)
    assert "conclusion" not in listing["run"]
    if expect_census:
        ids = [step.get("id") for step in steps]
        census = steps[ids.index("census")]
        assert "scripts.ci_run_batch census" in census["run"]
        assert ids.index("census") < ids.index(listing_step_id)
    cadence = _cadence_minutes(schedule_cron)
    env = job["env"]
    assert int(env["OVERLAP_MINUTES"]) >= 2 * cadence
    assert int(env["LOOKBACK_HOURS"]) * 60 >= lookback_floor_min
    concurrency = job.get("concurrency") or workflow.get("concurrency") or {}
    if concurrency:
        assert concurrency.get("cancel-in-progress") is False
        assert "${{" not in str(concurrency.get("group", "")), "one pass at a time, one group"
    _assert_daily_reconcile(workflow, job, env, listing_step_id, reconcile_cron)


def _assert_daily_reconcile(
    workflow: dict, job: dict, env: dict, listing_step_id: str, reconcile_cron: str
) -> None:
    if "RECONCILE" not in env:
        return
    crons = [entry["cron"] for entry in workflow[True]["schedule"]]
    assert reconcile_cron in crons
    reconciling = f"github.event.schedule == '{reconcile_cron}'"
    assert reconciling in env["RECONCILE"]
    timeout = str(job["timeout-minutes"])
    assert reconciling in timeout and "&& 30 ||" in timeout, "a reconcile pass gets 30 minutes"
    listing = next(step for step in job["steps"] if step.get("id") == listing_step_id)
    assert '[ "$RECONCILE" != "true" ] || reconcile=(--reconcile-horizon-days' in listing["run"]
    assert '"${reconcile[@]}"' in listing["run"]
    assert int(env["RECONCILE_HORIZON_DAYS"]) == 31


def test_stable_evidence_batch_selects_in_the_script_not_the_workflow() -> None:
    workflow = _workflow(STABLE_EVIDENCE)
    assert "workflow_run" not in workflow[True]
    job = workflow["jobs"]["append"]
    assert "if" not in job
    batch = next(step for step in job["steps"] if step.get("id") == "batch")
    assert "scripts.stable_evidence_batch" in batch["run"]
    assert "conclusion" not in batch["run"]
    _assert_batch_pass(
        workflow,
        "append",
        "stable-evidence.yml",
        240,
        "batch",
        "7-59/15 * * * *",
        "41 4 * * *",
        expect_census=True,
    )
    census = next(s for s in workflow["jobs"]["append"]["steps"] if s.get("id") == "census")
    hold_env = census["env"]
    names = {name for name in hold_env["WATCHED_WORKFLOWS"].split(",") if name}
    assert names == set(RECORDED_WORKFLOWS)
    assert workflow[True]["workflow_dispatch"]["inputs"]["reconcile"]["default"] is True


def test_trunk_job_verdict_skips_cancelled_triggering_run() -> None:
    workflow = _workflow(TRUNK_JOB_VERDICT)
    trigger = workflow[True]["workflow_run"]
    assert trigger["types"] == ["completed"]

    condition = _job_if(workflow, "verdict")
    assert "github.event.repository.default_branch" in condition
    assert CANCELLED_SKIP in condition


def test_error_sink_batch_lists_failures_without_conclusion_filters() -> None:
    workflow = _workflow(BUDGET_WATCH)
    job = workflow["jobs"]["error-sink"]
    listing = next(step for step in job["steps"] if step.get("id") == "sink")
    assert "scripts.ci_error_sink_batch" in listing["run"]
    mark = next(step for step in job["steps"] if step.get("id") == "mark")
    assert "--workflow-file ci-budget-watch.yml" in mark["run"]
    _assert_batch_pass(
        workflow,
        "error-sink",
        "ci-budget-watch.yml",
        180,
        "sink",
        "30 * * * *",
        "23 3 * * *",
        expect_census=False,
    )


def _token_env_read_by(module: str) -> str | None:
    source = (REPO / (module.replace(".", "/") + ".py")).read_text(encoding="utf-8")
    names = set(re.findall(r'os\.environ(?:\.get\(|\[)"(\w*TOKEN)"', source))
    assert len(names) <= 1, f"{module} reads {sorted(names)}; expected one token variable"
    return names.pop() if names else None


def test_every_batch_step_sets_the_token_variable_its_module_reads() -> None:
    checked = 0
    for path in (BUDGET_WATCH, STABLE_EVIDENCE):
        for job in _workflow(path)["jobs"].values():
            for step in job["steps"]:
                for module in re.findall(r"python3? -m (scripts\.\w+)", step.get("run", "")):
                    variable = _token_env_read_by(module)
                    if variable is None:
                        continue
                    assert "GITHUB_TOKEN" in str(step.get("env", {}).get(variable, "")) or (
                        "github.token" in str(step.get("env", {}).get(variable, ""))
                    ), f"{path.name} step {step.get('name')!r} runs {module} without {variable}"
                    checked += 1
    assert checked >= 5, checked
    assert {
        _token_env_read_by(m)
        for m in (
            "scripts.ci_error_sink_batch",
            "scripts.ci_run_batch",
            "scripts.stable_evidence_batch",
        )
    } == {"GITHUB_TOKEN"}
