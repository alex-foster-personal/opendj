#!/usr/bin/env python3
"""Classify a PR's Actions jobs as real evidence or a zero-step infrastructure refusal.

Issue #1166: a re-run replays a workflow run's ORIGINAL resolved configuration, not
today's. `gh run rerun` on a run created before the `CI_RUNS_ON_LINUX` repo variable
existed resurrects the retired `ubuntu-latest` fallback, no runner claims the job, and it
completes as `conclusion: failure` with zero recorded steps in a handful of seconds. In
`gh pr checks` and every GitHub UI this is indistinguishable from a real code failure, so
a human or a triage script reading only the top-line conclusion cannot tell "your code is
broken" from "no runner picked this up".

The distinguishing signal is the job's own step list: a job that executed any of its
steps has real test evidence, whatever its conclusion; a job that failed with an empty
`steps` array never ran anything and carries none. This never touches `runs-on`, the
`CI_RUNS_ON_*` variables, or which runner a job resolves to -- it only reads the jobs
already recorded for a PR's head SHA and reports which of them are evidence.

MINI-PRD
    R1 Zero-step legibility ...................................... done + ran + regression
       [if] a job's conclusion is `failure` and its `steps` array is empty
            [then] label it NO-RUNNER and exclude it from the failure tally [else stop]
       [if] a job executed one or more steps and its conclusion is `failure`
            [then] label it REAL-FAILURE and count it [else stop]
       [if] a job has not failed
            [then] label it OK regardless of step count [else stop]
       [if] the PR number, its head SHA, or the Actions jobs API cannot be read
            [then] exit with a precondition error, never a silent pass [else stop]

USAGE
    uv run --no-sync python -m scripts.ci_zero_step_triage <PR_NUMBER>
    just ci-zero-step-triage <PR_NUMBER>

-Claude
"""

from __future__ import annotations

import argparse

try:
    from scripts.ci_health_core import (
        EXIT_OK,
        EXIT_PRECONDITION,
        REPO,
        PreconditionError,
        _gh_api_json,
    )
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.ci_zero_step_triage") from None
    raise

# ----- configuration ---------------------------------------------------------------

PAGE_SIZE = 100

LABEL_NO_RUNNER = "NO-RUNNER"
LABEL_REAL_FAILURE = "REAL-FAILURE"
LABEL_OK = "OK"


# ----- GitHub reads ----------------------------------------------------------------


def _pr_head_sha(pr_number: int) -> str:
    """Read one PR's head SHA, refusing a malformed or missing response."""
    payload = _gh_api_json(f"repos/{REPO}/pulls/{pr_number}")
    head = payload.get("head") if isinstance(payload, dict) else None
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or not sha:
        raise PreconditionError(f"PR #{pr_number} has no readable head sha: {payload!r}")
    return sha


def _run_ids_at_head(head_sha: str) -> list[int]:
    """Every Actions run id recorded against this exact head SHA."""
    payload = _gh_api_json(f"repos/{REPO}/actions/runs?head_sha={head_sha}&per_page={PAGE_SIZE}")
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise PreconditionError(f"actions/runs for head {head_sha} has no workflow_runs list")
    run_ids: list[int] = []
    for run in payload["workflow_runs"]:
        if not isinstance(run, dict) or type(run.get("id")) is not int:
            raise PreconditionError(f"actions/runs for head {head_sha} contained an invalid run")
        run_ids.append(run["id"])
    return sorted(run_ids)


def _jobs_for_run(run_id: int) -> list[dict[str, object]]:
    """Every job of one run, keeping every field the Actions API returned."""
    payload = _gh_api_json(f"repos/{REPO}/actions/runs/{run_id}/jobs?per_page={PAGE_SIZE}")
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise PreconditionError(f"actions/runs/{run_id}/jobs has no jobs list")
    jobs: list[dict[str, object]] = []
    for item in payload["jobs"]:
        if not isinstance(item, dict) or "name" not in item or "conclusion" not in item:
            raise PreconditionError(f"actions/runs/{run_id}/jobs contained an invalid job")
        jobs.append(item)
    return jobs


# ----- classification ---------------------------------------------------------------


def _step_count(job: dict[str, object]) -> int:
    """The number of steps GitHub recorded for one job, refusing a malformed field."""
    steps = job.get("steps")
    if steps is None:
        return 0
    if not isinstance(steps, list):
        raise PreconditionError(f"job {job.get('id')!r} has a non-list steps field: {steps!r}")
    return len(steps)


def classify_job(job: dict[str, object]) -> tuple[str, int, str]:
    """Return (label, step_count, human reason) for one job dict from the Actions API."""
    name = job.get("name", "<unnamed job>")
    conclusion = job.get("conclusion")
    steps = _step_count(job)
    if conclusion == "failure" and steps == 0:
        return (
            LABEL_NO_RUNNER,
            steps,
            f'"{name}": failure with 0 steps executed -- infrastructure refusal '
            "(no runner claimed the job), not a code defect. Excluded from the "
            "failure tally below; never count this in a defect total.",
        )
    if conclusion == "failure":
        return LABEL_REAL_FAILURE, steps, f'"{name}": failure with {steps} step(s) executed.'
    return LABEL_OK, steps, f'"{name}": {conclusion}, {steps} step(s) executed.'


# ----- entry point -------------------------------------------------------------------


def main(pr_number: int) -> int:
    """Print one classified line per job at the PR's head and a summary tally."""
    head_sha = _pr_head_sha(pr_number)
    run_ids = _run_ids_at_head(head_sha)
    no_runner = 0
    real_failures = 0
    ok = 0
    for run_id in run_ids:
        for job in _jobs_for_run(run_id):
            label, _steps, reason = classify_job(job)
            print(f"[{label}] run={run_id} {reason}")
            if label == LABEL_NO_RUNNER:
                no_runner += 1
            elif label == LABEL_REAL_FAILURE:
                real_failures += 1
            else:
                ok += 1
    print(
        f"CI_ZERO_STEP_TRIAGE pr={pr_number} head={head_sha} runs={len(run_ids)} "
        f"no_runner={no_runner} real_failures={real_failures} ok={ok}"
    )
    return EXIT_OK


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr", type=int, help="pull request number to triage")
    args = parser.parse_args()
    try:
        raise SystemExit(main(args.pr))
    except PreconditionError as exc:
        print(f"[ERROR] precondition: {exc}")
        raise SystemExit(EXIT_PRECONDITION) from exc
