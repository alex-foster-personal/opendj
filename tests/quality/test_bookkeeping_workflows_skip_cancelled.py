"""Bookkeeping workflow_run jobs skip cancelled triggering runs.

Reads the shipped workflows, not a second hand-maintained representation.

Regression lines:
  - if stable-evidence's batch pass filters on nothing but status, reads its mark from
    a cache, or can fail to overlap the previous pass, then broken
  - if trunk-job-verdict verdict runs on a cancelled trunk run, then broken
  - if ci-cost-guard's batch pass filters on conclusion or reads its mark from a
    cache, then broken
  - if two consecutive passes can fail to overlap, then
    broken (issue #2505: the collapse was considered and rejected because the
    guard prices one run id per invocation with no cross-run aggregation)
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from scripts.stable_evidence_batch import RECORDED_WORKFLOWS

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


def _cadence_minutes(cron: str) -> int:
    match = re.fullmatch(r"(?:\*|\d+-59)/(\d+) \* \* \* \*", cron)
    assert match, f"not an every-N-minutes schedule: {cron}"
    return int(match.group(1))


def _assert_batch_pass(
    workflow: dict,
    job_name: str,
    workflow_file: str,
    lookback_floor_min: int,
    listing_step_id: str,
) -> None:
    """One scheduled pass, the mark read from GitHub's record of this workflow, overlap
    of at least two cadences, one concurrency group with no expression in it."""
    steps = workflow["jobs"][job_name]["steps"]
    mark = next(step for step in steps if step.get("id") == "mark")
    # if a failed pass advances the mark, or the search stops at a fixed window, then a run
    # of failed passes skips completions; the tested reader pages the whole history
    assert "scripts.ci_run_batch mark" in mark["run"], "the mark comes from the tested reader"
    assert f"--workflow-file {workflow_file}" in mark["run"]
    assert "per_page" not in mark["run"] and "failure" not in mark["run"]
    # if the pass can succeed while a watched run older than the lookback is in flight
    # then the mark steps past that run's completion and it is never listed again; and if
    # the census runs after the listing, a run completing between the two is in neither
    ids = [step.get("id") for step in steps]
    census = steps[ids.index("census")]
    assert "scripts.ci_run_batch census" in census["run"]
    assert '--lookback-hours "$LOOKBACK_HOURS"' in census["run"]
    assert ids.index("census") < ids.index(listing_step_id), "census before the listing"
    verdict = steps[-1]
    assert "steps.census.outputs.held" in str(verdict.get("env")), "the last step reads it"
    assert '[ "$HELD" = "0" ]' in verdict["run"]
    for step in (census, verdict):
        assert "if" not in step and "continue-on-error" not in step
    cadence = _cadence_minutes(workflow[True]["schedule"][0]["cron"])
    env = workflow["jobs"][job_name]["env"]
    assert int(env["OVERLAP_MINUTES"]) >= 2 * cadence
    assert int(env["LOOKBACK_HOURS"]) * 60 >= lookback_floor_min
    concurrency = workflow["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    assert "${{" not in str(concurrency["group"]), "one pass at a time, one group"
    _assert_daily_reconcile(workflow, workflow["jobs"][job_name], env, listing_step_id)


def _assert_daily_reconcile(workflow: dict, job: dict, env: dict, listing_step_id: str) -> None:
    """if a re-run of a run created before the lookback is never listed then its
    completion is lost; once a day the pass lists every re-run in GitHub's whole
    re-run window, with no mark a run of failed reconciles could strand, and with a
    timeout that fits the listing (30 days read 276 pages in 957 s)."""
    crons = [entry["cron"] for entry in workflow[True]["schedule"]]
    assert len(crons) == 2, "the plain cadence, then the daily reconcile"
    minute, hour, *rest = crons[1].split()
    assert minute.isdigit() and hour.isdigit() and rest == ["*", "*", "*"], "daily"
    reconciling = f"github.event.schedule == '{crons[1]}'"
    assert reconciling in env["RECONCILE"]
    assert "run-name" not in workflow, "no titled reconcile mark to find"
    assert workflow[True]["workflow_dispatch"]["inputs"]["reconcile"]["type"] == "boolean"
    timeout = str(job["timeout-minutes"])
    assert reconciling in timeout and "&& 30 ||" in timeout, "a reconcile pass gets 30 minutes"
    steps = job["steps"]
    assert not any("--display-title" in str(step.get("run")) for step in steps)
    listing = next(step for step in steps if step.get("id") == listing_step_id)
    assert '[ "$RECONCILE" != "true" ] || reconcile=(--reconcile-horizon-days' in listing["run"]
    assert '"${reconcile[@]}"' in listing["run"]
    assert int(env["RECONCILE_HORIZON_DAYS"]) == 31, "GitHub's 30-day re-run limit plus a day"


def test_stable_evidence_batch_selects_in_the_script_not_the_workflow() -> None:
    """The cancelled and `CI` dispatch exclusions live in select_suite_runs (pinned by
    tests/scripts/test_stable_evidence_batch.py); the workflow hands the script no
    conclusion, no event, and no per-run job condition."""
    workflow = _workflow(STABLE_EVIDENCE)
    assert "workflow_run" not in workflow[True]
    job = workflow["jobs"]["append"]
    assert "if" not in job
    batch = next(step for step in job["steps"] if step.get("id") == "batch")
    assert "scripts.stable_evidence_batch" in batch["run"]
    assert "conclusion" not in batch["run"]
    _assert_batch_pass(workflow, "append", "stable-evidence.yml", 240, "batch")
    census = next(s for s in workflow["jobs"]["append"]["steps"] if s.get("id") == "census")
    hold_env = census["env"]
    names = {name for name in hold_env["WATCHED_WORKFLOWS"].split(",") if name}
    assert names == set(RECORDED_WORKFLOWS), "the hold watches exactly what the pass records"
    assert workflow[True]["workflow_dispatch"]["inputs"]["reconcile"]["default"] is True, (
        "a bare dispatch is the release catch-up, and must see re-runs of older runs"
    )


def test_trunk_job_verdict_skips_cancelled_triggering_run() -> None:
    """if a trunk CI or E2E run is cancelled then the verdict job does not allocate a runner."""
    workflow = _workflow(TRUNK_JOB_VERDICT)
    trigger = workflow[True]["workflow_run"]
    assert trigger["types"] == ["completed"]

    condition = _job_if(workflow, "verdict")
    assert "github.event.repository.default_branch" in condition
    assert CANCELLED_SKIP in condition


def test_ci_cost_guard_batch_lists_every_completion_including_cancelled() -> None:
    """The batch pass filters on status=completed and never on conclusion.

    A run cancelled at its timeout bills the whole ceiling, which is the case
    the threshold is sized for (#2505). tests/test_ci_cost_guard_batch.py pins
    the selection; this pins that the workflow hands the script no conclusion
    filter and that the mark is read from GitHub's own record of this
    workflow, not from a cache that can be evicted.
    """
    workflow = _workflow(CI_COST_GUARD)
    steps = workflow["jobs"]["assess"]["steps"]
    price = next(step for step in steps if step.get("id") == "guard")
    assert "--batch" in price["run"]
    assert "python -m scripts.ci_cost_guard" in price["run"], (
        "the guard imports scripts.ci_run_batch, so run as a path it dies with "
        "ModuleNotFoundError before pricing anything"
    )
    assert "conclusion" not in price["run"]
    mark = next(step for step in steps if step.get("id") == "mark")
    assert "--workflow-file ci-cost-guard.yml" in mark["run"]
    alert = next(step for step in steps if step.get("name") == "Open cost alert issues")
    assert "exit 1" not in alert["run"], (
        "an alert must not fail the pass, or a new offender every cadence pins the mark"
    )


def _token_env_read_by(module: str) -> str:
    source = (REPO / (module.replace(".", "/") + ".py")).read_text(encoding="utf-8")
    names = set(re.findall(r'os\.environ(?:\.get\(|\[)"(\w*TOKEN)"', source))
    assert len(names) == 1, f"{module} reads {sorted(names)}; expected one token variable"
    return names.pop()


def test_every_batch_step_sets_the_token_variable_its_module_reads() -> None:
    """Each module's token variable is read out of its own source, so a step exporting
    another name fails here before a pass lists nothing. The guard read GH_TOKEN while the
    batch modules read GITHUB_TOKEN; reviewers misread the mix three times on #3844, so all
    three now read GITHUB_TOKEN, and this holds every step to it."""
    checked = 0
    for path in (CI_COST_GUARD, STABLE_EVIDENCE):
        for job in _workflow(path)["jobs"].values():
            for step in job["steps"]:
                for module in re.findall(r"python3? -m (scripts\.\w+)", step.get("run", "")):
                    variable = _token_env_read_by(module)
                    assert "GITHUB_TOKEN" in str(step.get("env", {}).get(variable, "")) or (
                        "github.token" in str(step.get("env", {}).get(variable, ""))
                    ), f"{path.name} step {step.get('name')!r} runs {module} without {variable}"
                    checked += 1
    assert checked >= 5, checked
    assert {
        _token_env_read_by(m)
        for m in ("scripts.ci_cost_guard", "scripts.ci_run_batch", "scripts.stable_evidence_batch")
    } == {"GITHUB_TOKEN"}


def test_ci_cost_guard_passes_overlap_by_at_least_one_cadence() -> None:
    """Two consecutive passes must overlap, so a late or failed pass loses nothing.

    The overlap floor (OVERLAP_MINUTES) must be at least twice the cron
    cadence, and the run creation lookback (LOOKBACK_HOURS) must exceed the
    longest watched workflow timeout, or a run created before the mark and
    completed after it is never priced. E2E's extended job alone can run 45 + 30 min.
    """
    _assert_batch_pass(_workflow(CI_COST_GUARD), "assess", "ci-cost-guard.yml", 120, "guard")
