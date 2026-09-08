#!/usr/bin/env python3
"""Fail when an open, non-docs pull request has no real Actions run at its head.

This runs outside ``pull_request`` because a missing pull-request delivery has
no in-workflow failure to report. It reads GitHub's live pull-request, file,
and Actions-run APIs with ``gh`` and exits nonzero for every uncovered head.

MINI-PRD
    R1 Head coverage ............................................. done + regression
       [if] an open PR changes a non-docs path and its head has no Actions run
            [then] exit 1 and name the PR and head SHA [else stop]
       [if] an open PR changes only excluded documentation paths
            [then] omit it from the required-CI denominator [else stop]
       [if] every Actions run at a head was skipped, cancelled, ended without
                 any job starting (action_required, stale, startup_failure, or a
                 zero-step runner refusal, issue #1166), or is still running
            [then] treat the head as uncovered and name it [else stop]
       [if] a completed run carries a conclusion outside GitHub's closed
                 workflow-run vocabulary
            [then] raise a precondition error instead of guessing whether it
                 tested the head [else stop]
       [if] an executed run is recorded beyond the newest 100 runs at a head
            [then] paginate the full listing and still find it [else stop]
       [if] GitHub returns an incomplete or malformed API response
            [then] exit 10 with an explicit precondition error [else stop]

-Claude
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

try:
    from scripts.ci_health_core import (
        EXIT_OK,
        EXIT_PRECONDITION,
        REPO,
        PreconditionError,
        _gh_api_json,
        _run_gh,
    )
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.pr_ci_coverage") from None
    raise

# ----- configuration ---------------------------------------------------------------

PAGE_SIZE = 100
MAX_API_WORKERS = 12
EXCLUDED_DOCS_PREFIXES = ("docs/", "handoffs/", ".planning/", "specs/", "blog/")
REQUIRED_RUN_EVENTS = frozenset(
    {"pull_request", "push", "workflow_dispatch", "repository_dispatch"}
)
# A workflow GitHub declines to execute still yields a run object carrying the triggering
# event, so the event alone proves only that a delivery arrived. These five conclusions
# mean no job was ever created, which is precisely the state this gate exists to catch:
# skipped (path-filtered out), cancelled before a runner picked a job up, action_required
# (fork PR or protected environment waiting on approval), stale (queued past expiry, never
# dispatched) and startup_failure (workflow file rejected before a job started). A head
# whose only runs end in one of these was never tested, however the delivery reached us.
NON_COVERING_CONCLUSIONS = frozenset(
    {"skipped", "cancelled", "action_required", "stale", "startup_failure"}
)
# A zero-step runner refusal (issue #1166) also completes as conclusion "failure": the
# job was never picked up by a runner, so it records zero executed steps and fails in a
# few seconds. Same top line as a genuine red run, no execution behind it. Failure alone
# is therefore not proof of execution; the run's jobs must show an executed step first.
CONCLUSION_FAILURE = "failure"
# The run conclusions that, on their own, prove jobs ran to an outcome. GitHub's
# workflow-run conclusion vocabulary is closed - the five above, these three, and
# failure - so a completed run whose conclusion is none of them is a payload this gate
# cannot reason about. _is_executed_run raises on it rather than guess whether the head
# was tested, closing the issue #1431 failure mode against conclusions we have not seen.
EXECUTED_CONCLUSIONS = frozenset({"success", "timed_out", "neutral"})
STATUS_CONTEXT = "PR head CI coverage"


# ----- GitHub reads ----------------------------------------------------------------


def _pages(path: str) -> Iterable[list[object]]:
    """Yield every paginated list, refusing malformed data rather than skipping it."""
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        payload = _gh_api_json(f"{path}{separator}per_page={PAGE_SIZE}&page={page}")
        if not isinstance(payload, list):
            raise PreconditionError(f"{path} page {page} was not a list")
        yield payload
        if len(payload) < PAGE_SIZE:
            return
        page += 1


def _open_prs() -> list[tuple[int, str]]:
    """Read every open PR with the minimum fields needed for a head check."""
    result: list[tuple[int, str]] = []
    for payload in _pages(f"repos/{REPO}/pulls?state=open"):
        for item in payload:
            if not isinstance(item, dict):
                raise PreconditionError("open pull-request listing contained a non-object")
            number = item.get("number")
            head = item.get("head")
            sha = head.get("sha") if isinstance(head, dict) else None
            if type(number) is not int or not isinstance(sha, str) or not sha:
                raise PreconditionError(
                    f"open pull-request listing has invalid number or head: {item!r}"
                )
            result.append((number, sha))
    return result


def _changed_files(pr_number: int) -> list[str]:
    """Read every changed filename for one PR, without a partial-page blind spot."""
    files: list[str] = []
    for payload in _pages(f"repos/{REPO}/pulls/{pr_number}/files"):
        for item in payload:
            filename = item.get("filename") if isinstance(item, dict) else None
            if not isinstance(filename, str) or not filename:
                raise PreconditionError(
                    f"PR #{pr_number} files response has invalid filename: {item!r}"
                )
            files.append(filename)
    if not files:
        raise PreconditionError(f"PR #{pr_number} has no changed files")
    return files


def _validated_jobs(payload: object, run_id: int) -> list[dict[str, object]]:
    """Return one actions/runs/{id}/jobs page's jobs, refusing a malformed body."""
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise PreconditionError(f"actions/runs/{run_id}/jobs response has no jobs list")
    jobs = payload["jobs"]
    for index, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            raise PreconditionError(
                f"actions/runs/{run_id}/jobs response job {index} was not an object"
            )
    return jobs


def _jobs_prove_execution(jobs: list[dict[str, object]], run_id: int) -> bool:
    """Return whether any job recorded an executed step, refusing a mangled one.

    The step list is the record of whether a runner picked the job up: a job a runner
    executed always lists the steps it ran, while the zero-step refusal (issue #1166)
    reports none on every job. A malformed steps field is never read as "no execution",
    so a missing or non-list one raises exactly as the sibling ci_zero_step_triage does;
    a job that never ran reports an empty list, never an absent one.
    """
    for index, job in enumerate(jobs, start=1):
        steps = job.get("steps")
        if steps is None:
            raise PreconditionError(
                f"actions/runs/{run_id}/jobs response job {index} omits its steps "
                f"field; cannot read whether it executed"
            )
        if not isinstance(steps, list):
            raise PreconditionError(
                f"actions/runs/{run_id}/jobs response job {index} has a non-list "
                f"steps field: {steps!r}"
            )
        if steps:
            return True
    return False


def _jobs_payload_proves_execution(payload: object, run_id: int) -> bool:
    """One jobs page body, validated and asked whether any job actually executed."""
    return _jobs_prove_execution(_validated_jobs(payload, run_id), run_id)


def _run_executed_any_job(run_id: int) -> bool:
    """Return whether one run's jobs show an executed step, paging like ``_pages``.

    Called only for a run whose failure conclusion is also the zero-step runner-refusal
    signature. Early-returns on the first executed step; when every job so far has none,
    pages on until a short page so a truncated read is never mistaken for "nothing ran".
    """
    path = f"repos/{REPO}/actions/runs/{run_id}/jobs"
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        payload = _gh_api_json(f"{path}{separator}per_page={PAGE_SIZE}&page={page}")
        jobs = _validated_jobs(payload, run_id)
        if _jobs_prove_execution(jobs, run_id):
            return True
        if len(jobs) < PAGE_SIZE:
            return False
        page += 1


def _is_executed_run(run: dict[str, object], head_sha: str) -> bool:
    """Return whether one run object is evidence that a workflow actually executed here.

    A completed conclusion other than ``failure`` is execution proof on the run object
    alone. ``failure`` is ambiguous: it is also the conclusion of a zero-step runner
    refusal whose jobs never ran (issue #1166), indistinguishable from a genuine red
    run by conclusion alone. A failing run is therefore accepted only once its jobs
    record an executed step, preserving the intended acceptance of real failed CI.
    """
    if run.get("head_sha") != head_sha or run.get("event") not in REQUIRED_RUN_EVENTS:
        return False
    conclusion = run.get("conclusion")
    if conclusion is None and run.get("status") != "completed":
        # Queued or running. An outcome may arrive later; there is none to trust now.
        return False
    if not isinstance(conclusion, str) or not conclusion:
        raise PreconditionError(
            f"Actions run {run.get('id')!r} at {head_sha} reports status "
            f"{run.get('status')!r} with conclusion {conclusion!r}"
        )
    if conclusion in NON_COVERING_CONCLUSIONS:
        return False
    if conclusion == CONCLUSION_FAILURE:
        run_id = run.get("id")
        if type(run_id) is not int:
            raise PreconditionError(f"Actions run at {head_sha} has an invalid id: {run_id!r}")
        return _run_executed_any_job(run_id)
    if conclusion not in EXECUTED_CONCLUSIONS:
        raise PreconditionError(
            f"Actions run {run.get('id')!r} at {head_sha} has an unrecognized "
            f"conclusion {conclusion!r}; cannot say whether it tested the head"
        )
    return True


def _has_actions_run_at_head(head_sha: str) -> bool:
    """Return whether a non-create Actions workflow executed against this exact head.

    Reads every page of the head's actions/runs listing: only the newest 100 runs would
    let an executed run recorded earlier slip past when newer runs are non-covering.
    Follows ``_pages``' short-page stop and validates every page fail-closed, accepting
    the first run that proves execution.
    """
    path = f"repos/{REPO}/actions/runs?head_sha={head_sha}"
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        payload = _gh_api_json(f"{path}{separator}per_page={PAGE_SIZE}&page={page}")
        if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
            raise PreconditionError(
                f"Actions runs response for {head_sha} page {page} has no workflow_runs list"
            )
        runs = payload["workflow_runs"]
        for run in runs:
            if not isinstance(run, dict):
                raise PreconditionError(
                    f"Actions runs response for {head_sha} page {page} contained a non-object"
                )
            if _is_executed_run(run, head_sha):
                return True
        if len(runs) < PAGE_SIZE:
            return False
        page += 1


# ----- verdict ---------------------------------------------------------------------


def _is_docs_path(path: str) -> bool:
    """Match ci.yml's docs-only exclusions, including its requirements exception."""
    if path == ".planning/REQUIREMENTS.md":
        return False
    return path.endswith(".md") or path.startswith(EXCLUDED_DOCS_PREFIXES)


def _requires_ci(files: list[str]) -> bool:
    """A PR needs CI if any changed file is outside ci.yml's docs exclusions."""
    return any(not _is_docs_path(path) for path in files)


def _inspect_pr(pr: tuple[int, str]) -> tuple[int, str, bool, bool]:
    """Return one PR's number, head, docs-only state, and head-run coverage."""
    number, head_sha = pr
    requires_ci = _requires_ci(_changed_files(number))
    return number, head_sha, requires_ci, requires_ci and _has_actions_run_at_head(head_sha)


def _publish_head_status(inspection: tuple[int, str, bool, bool]) -> None:
    """Attach an explicit success or failure status to a checked non-docs PR head."""
    number, head_sha, requires_ci, covered = inspection
    if not requires_ci:
        return
    state = "success" if covered else "failure"
    description = "Actions run found at PR head" if covered else "No non-bot Actions run at PR head"
    _run_gh(
        [
            "api",
            "--method",
            "POST",
            f"repos/{REPO}/statuses/{head_sha}",
            "-f",
            f"state={state}",
            "-f",
            f"context={STATUS_CONTEXT}",
            "-f",
            f"description={description} for PR #{number}",
        ]
    )


def main(*, publish_status: bool) -> int:
    """Print named denominators and fail for every untested, non-docs PR head."""
    required = 0
    skipped_docs_only = 0
    uncovered: list[tuple[int, str]] = []
    prs = _open_prs()
    with ThreadPoolExecutor(max_workers=MAX_API_WORKERS) as executor:
        inspections = list(executor.map(_inspect_pr, prs))
    if publish_status:
        with ThreadPoolExecutor(max_workers=MAX_API_WORKERS) as executor:
            list(executor.map(_publish_head_status, inspections))
    for number, head_sha, requires_ci, covered in inspections:
        if not requires_ci:
            skipped_docs_only += 1
        elif covered:
            required += 1
        else:
            required += 1
            uncovered.append((number, head_sha))

    print(
        "PR_CI_COVERAGE "
        f"open_non_docs={required} docs_only_skipped={skipped_docs_only} uncovered={len(uncovered)}"
    )
    for number, head_sha in uncovered:
        print(f"[ERROR] PR #{number} head {head_sha} has no non-bot Actions run")
    return EXIT_OK if not uncovered else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish-status",
        action="store_true",
        help="publish the explicit PR-head status contexts after inspecting GitHub",
    )
    args = parser.parse_args()
    try:
        raise SystemExit(main(publish_status=args.publish_status))
    except PreconditionError as exc:
        print(f"[ERROR] precondition: {exc}")
        raise SystemExit(EXIT_PRECONDITION) from exc
