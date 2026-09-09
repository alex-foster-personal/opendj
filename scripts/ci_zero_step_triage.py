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

Post-merge hardening (PR #1417 review, Sun 6 Sep 2026): the original catch-all treated
"not exactly failure" as OK, so a still-running job (`conclusion: null`), a cancelled
job, and a `timed_out` job all landed in the `ok` tally and read as clean. `classify_job`
now names every GitHub-defined conclusion explicitly and refuses (`PreconditionError`)
on anything it does not recognise, per the repo's verification rule that a check tested
for not-failure is a defect. Both list reads paginate against the API's own
`total_count` instead of trusting one page.

Post-merge hardening (PR #1420 review, Mon 7 Sep 2026): that same PR #1417 fix flagged
double-counting once `workflow_dispatch` guarantees a second run per push, and added
`_latest_run_per_workflow` to keep only the newest run id per `workflow_id`. That was
wrong: GitHub records a separate run per triggering event (push, pull_request, and now
workflow_dispatch) at one head SHA, and all of them share one `workflow_id`, so keeping
only the newest run id silently discarded any REAL-FAILURE or NO-RUNNER run superseded
by a later, unrelated trigger -- exactly the case this module exists to catch, not a
duplicate to dedup away. Every run id GitHub returns for a head SHA is now tallied;
run ids are already unique per `_runs_at_head`, so no further dedup is needed.

MINI-PRD
    R1 Zero-step legibility ...................................... done + ran + regression
       [if] a job's conclusion is `failure` and its `steps` array is empty
            [then] label it NO-RUNNER and exclude it from the failure tally [else stop]
       [if] a job executed one or more steps and its conclusion is `failure`
            [then] label it REAL-FAILURE and count it [else stop]
       [if] a job's conclusion is `success`
            [then] label it OK regardless of step count [else stop]
       [if] a job's conclusion is `skipped`
            [then] label it SKIPPED and tally it separately from `ok` -- a job that
                   ran nothing is not evidence anything passed [else stop]
       [if] a job's conclusion is `timed_out`
            [then] label it TIMED-OUT and count it with the real failures [else stop]
       [if] a job's conclusion is `cancelled`, or is still `null` (queued/in progress)
            [then] label it CANCELLED or INCOMPLETE and exclude it from every tally,
                   never default it to OK [else stop]
       [if] a job's conclusion is `action_required`, `neutral`, or `stale`
            [then] label it by that name and exclude it from the ok tally [else stop]
       [if] a job's conclusion is anything else GitHub can report
            [then] raise a precondition error, never guess OK [else stop]
       [if] the PR number, its head SHA, or the Actions jobs API cannot be read
            [then] exit with a precondition error, never a silent pass [else stop]
    R2 Paginated, never-dropped tally ............................. done + ran + regression
       [if] a head SHA has more recorded runs, or a run has more jobs, than one API page
            [then] every page is read and the list is refused if it falls short of the
                   endpoint's own total_count, never silently truncated [else stop]
       [if] more than one run is recorded at the head SHA (a push/pull_request run
                 alongside a later workflow_dispatch, or two runs of one workflow_id)
            [then] every run's jobs are tallied -- dedup by workflow_id or by time
                   never hides a real failure [else stop]
       [if] the Actions runs list contains a non-dict element
            [then] raise a precondition error, never call .get on it [else stop]

USAGE
    uv run --no-sync python -m scripts.ci_zero_step_triage <PR_NUMBER>
    just ci-zero-step-triage <PR_NUMBER>

-Claude
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

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
LABEL_TIMED_OUT = "TIMED-OUT"
LABEL_OK = "OK"
LABEL_SKIPPED = "SKIPPED"
LABEL_CANCELLED = "CANCELLED"
LABEL_INCOMPLETE = "INCOMPLETE"
LABEL_ACTION_REQUIRED = "ACTION-REQUIRED"
LABEL_NEUTRAL = "NEUTRAL"
LABEL_STALE = "STALE"

# Labels that are neither OK nor a counted failure -- unmeasurable, excluded from both.
EXCLUDED_LABELS = (
    LABEL_CANCELLED,
    LABEL_INCOMPLETE,
    LABEL_ACTION_REQUIRED,
    LABEL_NEUTRAL,
    LABEL_STALE,
)


# ----- GitHub reads ----------------------------------------------------------------


def _pr_head_sha(pr_number: int) -> str:
    """Read one PR's head SHA, refusing a malformed or missing response."""
    payload = _gh_api_json(f"repos/{REPO}/pulls/{pr_number}")
    head = payload.get("head") if isinstance(payload, dict) else None
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or not sha:
        raise PreconditionError(f"PR #{pr_number} has no readable head sha: {payload!r}")
    return sha


def _paginated_list(path: str, list_key: str) -> list[dict[str, object]]:
    """Follow every page of a GitHub list endpoint, refusing a truncated read.

    A single-page read silently drops anything past `PAGE_SIZE`: a head SHA with more
    than 100 recorded runs, or a sharded run with more than 100 jobs, would otherwise
    vanish from the tally with no signal. This keeps requesting pages until the
    endpoint's own `total_count` is fully accounted for, and raises rather than
    returning a short list if it never catches up.
    """
    sep = "&" if "?" in path else "?"
    items: list[dict[str, object]] = []
    total_count: int | None = None
    page = 1
    while True:
        payload = _gh_api_json(f"{path}{sep}per_page={PAGE_SIZE}&page={page}")
        if not isinstance(payload, dict) or not isinstance(payload.get(list_key), list):
            raise PreconditionError(f"{path} page {page} has no {list_key!r} list: {payload!r}")
        if total_count is None:
            total_count = payload.get("total_count")
            if not isinstance(total_count, int):
                raise PreconditionError(f"{path} response has no integer total_count")
        page_items = payload[list_key]
        if not page_items:
            break
        items.extend(page_items)
        if len(items) >= total_count:
            break
        page += 1
    if len(items) != total_count:
        raise PreconditionError(
            f"{path} reported total_count={total_count} but paging collected {len(items)} "
            f"{list_key} -- refusing a truncated tally"
        )
    return items


@dataclass(frozen=True)
class _RunRef:
    """One workflow run recorded against a head SHA, reduced to what dedup needs."""

    run_id: int
    workflow_id: int


def _runs_at_head(head_sha: str) -> list[_RunRef]:
    """Every Actions run recorded against this exact head SHA, fully paginated."""
    runs: list[_RunRef] = []
    for run in _paginated_list(f"repos/{REPO}/actions/runs?head_sha={head_sha}", "workflow_runs"):
        if not isinstance(run, dict):
            raise PreconditionError(
                f"actions/runs for head {head_sha} contained an invalid run: {run!r}"
            )
        run_id = run.get("id")
        workflow_id = run.get("workflow_id")
        if type(run_id) is not int or type(workflow_id) is not int:
            raise PreconditionError(
                f"actions/runs for head {head_sha} contained an invalid run: {run!r}"
            )
        runs.append(_RunRef(run_id=run_id, workflow_id=workflow_id))
    return runs


def _run_ids_to_tally(runs: list[_RunRef]) -> list[int]:
    """Every run id recorded at the head SHA, never deduped down to one per workflow.

    GitHub records a separate run per triggering event -- push, pull_request, and
    workflow_dispatch can each produce their own run for one identical head SHA, and
    all of them share one `workflow_id`. A prior fix (PR #1417) kept only the newest
    run id per `workflow_id`, reasoning that a `workflow_dispatch` re-run duplicates
    the original push/pull_request run's jobs. That silently discarded a REAL-FAILURE
    or NO-RUNNER run whenever a later, unrelated trigger happened to share the same
    workflow at the same head -- the run ids are for genuinely distinct runs, so
    nothing is a true duplicate to dedup away. `_runs_at_head` already returns one
    `_RunRef` per run id (GitHub's own list is not paginated across duplicates), so
    every id it returns is tallied, sorted only for deterministic output.
    """
    return sorted(run.run_id for run in runs)


def _jobs_for_run(run_id: int) -> list[dict[str, object]]:
    """Every job of one run, fully paginated, keeping every field the Actions API returned."""
    jobs: list[dict[str, object]] = []
    for item in _paginated_list(f"repos/{REPO}/actions/runs/{run_id}/jobs", "jobs"):
        if not isinstance(item, dict) or "name" not in item or "conclusion" not in item:
            raise PreconditionError(
                f"actions/runs/{run_id}/jobs contained an invalid job: {item!r}"
            )
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
    """Return (label, step_count, human reason) for one job dict from the Actions API.

    Every conclusion GitHub defines is named explicitly. Nothing falls through to OK by
    default -- an unrecognised conclusion is a precondition failure, never a guess.
    """
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
    if conclusion == "timed_out":
        return (
            LABEL_TIMED_OUT,
            steps,
            f'"{name}": timed_out with {steps} step(s) executed -- counted with the real failures.',
        )
    if conclusion == "cancelled":
        return (
            LABEL_CANCELLED,
            steps,
            f'"{name}": cancelled, {steps} step(s) executed -- unmeasurable, '
            "excluded from every tally.",
        )
    if conclusion is None:
        return (
            LABEL_INCOMPLETE,
            steps,
            f'"{name}": still queued or in progress -- excluded from every tally, '
            "re-run once it completes.",
        )
    if conclusion in ("action_required", "neutral", "stale"):
        labels = {
            "action_required": LABEL_ACTION_REQUIRED,
            "neutral": LABEL_NEUTRAL,
            "stale": LABEL_STALE,
        }
        label = labels[conclusion]
        return (
            label,
            steps,
            f'"{name}": {conclusion}, {steps} step(s) executed -- excluded from the ok tally.',
        )
    if conclusion == "skipped":
        return (
            LABEL_SKIPPED,
            steps,
            f'"{name}": skipped, {steps} step(s) executed -- ran nothing, not evidence '
            "anything passed. Tallied separately from ok.",
        )
    if conclusion == "success":
        return LABEL_OK, steps, f'"{name}": {conclusion}, {steps} step(s) executed.'
    raise PreconditionError(
        f'job {job.get("id")!r} named {name!r} has an unrecognized conclusion {conclusion!r} -- '
        "refusing to default it to OK"
    )


# ----- entry point -------------------------------------------------------------------


def main(pr_number: int) -> int:
    """Print one classified line per job at the PR's head and a summary tally."""
    head_sha = _pr_head_sha(pr_number)
    run_ids = _run_ids_to_tally(_runs_at_head(head_sha))
    no_runner = 0
    real_failures = 0
    ok = 0
    skipped = 0
    excluded = 0
    for run_id in run_ids:
        for job in _jobs_for_run(run_id):
            label, _steps, reason = classify_job(job)
            print(f"[{label}] run={run_id} {reason}")
            if label == LABEL_NO_RUNNER:
                no_runner += 1
            elif label in (LABEL_REAL_FAILURE, LABEL_TIMED_OUT):
                real_failures += 1
            elif label == LABEL_OK:
                ok += 1
            elif label == LABEL_SKIPPED:
                skipped += 1
            elif label in EXCLUDED_LABELS:
                excluded += 1
            else:  # pragma: no cover -- classify_job only returns the labels handled above
                raise PreconditionError(f"classify_job returned an untallied label: {label!r}")
    print(
        f"CI_ZERO_STEP_TRIAGE pr={pr_number} head={head_sha} runs={len(run_ids)} "
        f"no_runner={no_runner} real_failures={real_failures} ok={ok} skipped={skipped} "
        f"excluded={excluded}"
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
