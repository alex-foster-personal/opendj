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
       [if] a files listing is empty [then] see scripts/pr_ci_coverage_files.py (PR #3771)
       [if] an open non-docs PR head has mergeable_state dirty [then] name it unbuildable,
            not uncovered, and do not fail the run [else stop] (issue #1782)
    R2 Merge-race correction ..................................... done + regression
       [if] a PR's head was stamped failure while open, then gains a qualifying run
            and merges before the next scheduled watchdog run
            [then] the very next run still republishes success on that exact head,
                 with a fresh status timestamp [else stop] (issue #1368)
       [if] a closed PR was never merged (declined, superseded)
            [then] it is never re-inspected, whatever its update time [else stop]
       [if] the closed-PR listing page is older than the recheck window
            [then] pagination stops there rather than reading the whole PR history
                 [else stop]
       [if] any PR is inspected, open or recently-merged
            [then] the run log names it and the verdict reached, so a silent skip
                 shows up as a missing line rather than an absence [else stop]
       [if] one PR's inspection raises inside the pool
            [then] every verdict already reached is logged and the raising PR is
                 named before the failure ends the run, publishing nothing
                 [else stop]

-Claude
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

try:
    from scripts.ci_health_core import (
        EXIT_OK,
        EXIT_PRECONDITION,
        REPO,
        PreconditionError,
        _gh_api_json,
        _parse_github_timestamp,
        _run_gh,
    )
    from scripts.pr_ci_coverage_files import require_corroborated_empty_diff
    from scripts.review_docs_only import is_docs_only
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.pr_ci_coverage") from None
    raise

# ----- configuration ---------------------------------------------------------------

PAGE_SIZE = 100
MAX_API_WORKERS = 12
# ci-budget-watch.yml schedules this script every 12h. A PR can merge in the gap
# between two runs, dropping out of `pulls?state=open` before its head is ever
# re-checked - the exact shape of issue #1368, where a status posted `failure`
# minutes before a merge stayed `failure` forever because nothing looked again.
# Double the cadence so the very next scheduled run, not just a lucky one, always
# still sees a PR that merged since the last run.
RECHECK_WINDOW_HOURS = 24


def _now() -> datetime:
    """Wall clock, isolated so tests can pin it to a real captured moment."""
    return datetime.now(UTC)

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
Inspection = tuple[int, str, bool, bool, bool]
OpenPr = tuple[int, str, str | None]


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


def _open_prs() -> list[OpenPr]:
    """Read every open PR with the minimum fields needed for a head check."""
    result: list[OpenPr] = []
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
            mergeable_state = item.get("mergeable_state")
            if mergeable_state is not None and not isinstance(mergeable_state, str):
                raise PreconditionError(
                    f"PR #{number} has a non-string mergeable_state: {mergeable_state!r}"
                )
            result.append((number, sha, mergeable_state))
    return result


def _pr_timestamp(raw: object, field: str, number: int) -> datetime:
    """Validate a PR listing timestamp field is a string before parsing it."""
    if not isinstance(raw, str):
        raise PreconditionError(f"PR #{number} has a missing or non-string {field}: {raw!r}")
    return _parse_github_timestamp(raw, field, f"PR #{number}")


def _recently_merged_prs(cutoff: datetime) -> list[tuple[int, str]]:
    """Read merged PRs updated at or after `cutoff`, giving a merged head one more look.

    `_open_prs` alone is what let issue #1368 happen: a PR that merges between two
    scheduled runs drops out of `state=open` and is never inspected again, so a
    `failure` status stamped on its head minutes before the merge is stuck forever
    even once a real run lands there. `pulls?state=closed&sort=updated&direction=desc`
    returns newest-updated first, and a merge always bumps `updated_at` to at least
    `merged_at` (nothing can touch a PR before it merges), so once a page's item is
    older than `cutoff` every later item is provably older too - pagination can stop
    there instead of reading the PR's whole history for one recheck window.
    """
    result: list[tuple[int, str]] = []
    path = f"repos/{REPO}/pulls?state=closed&sort=updated&direction=desc"
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        payload = _gh_api_json(f"{path}{separator}per_page={PAGE_SIZE}&page={page}")
        if not isinstance(payload, list):
            raise PreconditionError(f"{path} page {page} was not a list")
        for item in payload:
            if not isinstance(item, dict):
                raise PreconditionError("closed pull-request listing contained a non-object")
            number = item.get("number")
            head = item.get("head")
            sha = head.get("sha") if isinstance(head, dict) else None
            if type(number) is not int or not isinstance(sha, str) or not sha:
                raise PreconditionError(
                    f"closed pull-request listing has invalid number or head: {item!r}"
                )
            updated_at = _pr_timestamp(item.get("updated_at"), "updated_at", number)
            if updated_at < cutoff:
                return result
            merged_at_raw = item.get("merged_at")
            if merged_at_raw is not None:
                merged_at = _pr_timestamp(merged_at_raw, "merged_at", number)
                if merged_at >= cutoff:
                    result.append((number, sha))
        if len(payload) < PAGE_SIZE:
            return result
        page += 1


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
        require_corroborated_empty_diff(pr_number, _gh_api_json(f"repos/{REPO}/pulls/{pr_number}"))
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


def _requires_ci(files: list[str]) -> bool:
    """A PR needs CI if any changed file is outside ci.yml's docs exclusions.

    Exactly ``not is_docs_only``, so ``[]`` still requires CI (a PROVEN-empty diff is
    ``_inspect_pr``'s call). Delegates to ``scripts.review_docs_only.is_docs_only``, which
    parses ci.yml's own `pull_request.paths` filter, instead of a
    hand-maintained prefix list that could drift from it.
    """
    return not is_docs_only(files)


def _inspect_pr(pr: tuple[int, str] | OpenPr) -> Inspection:
    """Return one PR's number, head, docs-only state, coverage, and unbuildable flag."""
    number, head_sha, mergeable_state = (*pr, None)[:3]
    unbuildable = mergeable_state == "dirty"
    files = _changed_files(number)
    requires_ci = bool(files) and _requires_ci(files)
    covered = False if unbuildable or not requires_ci else _has_actions_run_at_head(head_sha)
    return number, head_sha, requires_ci, covered, unbuildable


def _publish_head_status(inspection: Inspection) -> None:
    """Attach an explicit success or failure status to a checked non-docs PR head.

    Prints a confirmed-outcome line only once `_run_gh` actually returns: if the
    POST raises, this PR gets no confirmation line, so a partial publication is
    truthfully distinguishable from a completed one in the log, not just inferred
    from the pre-POST intent line in `_log_inspection`.
    """
    number, head_sha, requires_ci, covered, unbuildable = inspection
    if not requires_ci or unbuildable:
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
    print(f"PR_CI_COVERAGE_PUBLISHED pr=#{number} head={head_sha} state={state}")


def _log_inspection(scope: str, inspection: Inspection, *, publish_status: bool) -> None:
    """Name one inspected PR and the verdict this run reached, so a silent skip shows
    up as a missing log line rather than as an absence nobody can distinguish from
    "never inspected" (issue #1368's second acceptance criterion).

    `publish_attempted` states intent to publish, decided before any POST is
    attempted - it is not a confirmed outcome. `_publish_head_status` prints its
    own `PR_CI_COVERAGE_PUBLISHED` line once a POST actually succeeds, so a
    reader can tell an attempted-but-failed publish apart from a confirmed one.
    """
    number, head_sha, requires_ci, covered, unbuildable = inspection
    if not requires_ci:
        verdict = "docs-only-skipped"
    elif unbuildable:
        verdict = "unbuildable"
    elif covered:
        verdict = "success"
    else:
        verdict = "failure"
    print(
        f"PR_CI_COVERAGE_PR scope={scope} pr=#{number} head={head_sha} "
        f"verdict={verdict} publish_attempted={publish_status and requires_ci}"
    )


def _inspect_all(
    prs: list[tuple[int, str] | OpenPr],
) -> tuple[list[Inspection | None], BaseException | None]:
    """Inspect every PR concurrently, keeping the verdicts that DID complete.

    `list(executor.map(...))` re-raises on the first broken future, discarding every
    result behind it - so one PR returning a malformed payload used to erase the whole
    run's evidence, leaving PRs that were inspected successfully indistinguishable from
    PRs that were never looked at (the exact absence issue #1368's second acceptance
    criterion exists to rule out). Each future is therefore consumed individually, via
    `Future.exception()` so no `except` clause here can ever swallow one.

    The failure is RETAINED, never handled: the caller logs what completed and then
    re-raises it, so the run still ends loudly and publishes nothing from a partial
    inspection set.
    """
    with ThreadPoolExecutor(max_workers=MAX_API_WORKERS) as executor:
        futures = [executor.submit(_inspect_pr, pr) for pr in prs]
    inspections: list[Inspection | None] = []
    failure: BaseException | None = None
    for future in futures:
        raised = future.exception()
        if raised is None:
            inspections.append(future.result())
            continue
        inspections.append(None)
        if failure is None:
            failure = raised
    return inspections, failure


def _log_failed_inspection(scope: str, pr: tuple[int, str] | OpenPr) -> None:
    """Name a PR whose own inspection raised, rather than leaving it out of the log.

    Its verdict is unknown, so it is reported as one - `inspection-error` is not a
    coverage verdict and never enters a denominator. Without this line the failing
    PR would be the only one absent from the log, which is the shape a silent skip
    also has.
    """
    number = pr[0]
    head_sha = pr[1]
    print(
        f"PR_CI_COVERAGE_PR scope={scope} pr=#{number} head={head_sha} "
        "verdict=inspection-error publish_attempted=False"
    )


def _tally_open_inspections(
    open_prs: list[OpenPr],
    inspections: list[Inspection | None],
    *,
    publish_status: bool,
) -> tuple[int, int, list[tuple[int, str]], list[tuple[int, str]]]:
    """Log open-scope inspections and return docs-only skip plus uncovered/unbuildable lists."""
    required = 0
    skipped_docs_only = 0
    uncovered: list[tuple[int, str]] = []
    unbuildable: list[tuple[int, str]] = []
    for pr, inspection in zip(open_prs, inspections, strict=True):
        if inspection is None:
            _log_failed_inspection("open", pr)
            continue
        _log_inspection("open", inspection, publish_status=publish_status)
        number, head_sha, requires_ci, covered, head_unbuildable = inspection
        if not requires_ci:
            skipped_docs_only += 1
        elif head_unbuildable:
            required += 1
            unbuildable.append((number, head_sha))
        elif covered:
            required += 1
        else:
            required += 1
            uncovered.append((number, head_sha))
    return required, skipped_docs_only, uncovered, unbuildable


def main(*, publish_status: bool, now: datetime | None = None) -> int:
    """Print named denominators and fail for every untested, non-docs open PR head.

    Also rechecks PRs merged within `RECHECK_WINDOW_HOURS`: a head stamped `failure`
    while open must still be correctable once a real run lands, even if the PR merged
    in the gap before the next scheduled run saw it (issue #1368). A merged head's
    verdict never affects the exit code below - it can no longer block anything - but
    it is always logged and always republished when `--publish-status` is set.

    `now` is an explicit dependency-injection seam for tests that must pin the
    recheck cutoff to a real historical moment (real GitHub data captured once is
    inert against a moving wall clock). Production always omits it and gets the
    real `_now()`; this keeps the seam a parameter, never a monkeypatched
    module function.
    """
    cutoff = (now if now is not None else _now()) - timedelta(hours=RECHECK_WINDOW_HOURS)
    open_prs = _open_prs()
    recheck_prs = _recently_merged_prs(cutoff)
    inspections, inspection_failure = _inspect_all(open_prs + recheck_prs)

    # Every inspection is logged before any status POST is attempted, and before an
    # inspection failure is re-raised: neither a transient publish failure nor one
    # PR's broken payload may erase the acceptance guarantee that every inspected PR
    # is named (issue #1368's second acceptance criterion).
    # `publish_attempted` in each log line records intent to publish, not a
    # confirmed POST outcome - `_publish_head_status` prints its own confirmation
    # line once a POST actually succeeds.
    split = len(open_prs)
    required, skipped_docs_only, uncovered, unbuildable = _tally_open_inspections(
        open_prs, inspections[:split], publish_status=publish_status
    )

    rechecked = 0
    recovered = 0
    for pr, inspection in zip(recheck_prs, inspections[split:], strict=True):
        if inspection is None:
            _log_failed_inspection("recheck", pr)
            continue
        _log_inspection("recheck", inspection, publish_status=publish_status)
        _, _, requires_ci, covered, _head_unbuildable = inspection
        if requires_ci:
            rechecked += 1
            recovered += 1 if covered else 0

    if inspection_failure is not None:
        # The inspection set is incomplete, so every count below would be measured
        # against a denominator missing rows nobody can enumerate, and no status may
        # be published from a partial set. Flush first: stdout is block-buffered when
        # the watchdog's output is piped, so an unflushed exit would throw away the
        # very verdict lines just printed. Then the original failure propagates
        # unswallowed, exactly as it did before it was retained.
        sys.stdout.flush()
        raise inspection_failure

    print(
        "PR_CI_COVERAGE "
        f"open_non_docs={required} docs_only_skipped={skipped_docs_only} "
        f"uncovered={len(uncovered)} unbuildable={len(unbuildable)} "
        f"recheck_candidates={len(recheck_prs)} "
        f"recheck_non_docs={rechecked} recheck_covered={recovered}"
    )
    for number, head_sha in unbuildable:
        print(f"[INFO] PR #{number} head {head_sha} merge ref unbuildable; CI not dispatched")
    for number, head_sha in uncovered:
        print(f"[ERROR] PR #{number} head {head_sha} has no non-bot Actions run")

    if publish_status:
        # Every entry is non-None here: the raise above is what makes that true, and
        # this comprehension is that invariant stated in the type rather than assumed.
        complete = [inspection for inspection in inspections if inspection is not None]
        with ThreadPoolExecutor(max_workers=MAX_API_WORKERS) as executor:
            list(executor.map(_publish_head_status, complete))

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
