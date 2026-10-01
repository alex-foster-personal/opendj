#!/usr/bin/env python3
"""Cancel superseded queued trunk CI and bookkeeping workflow runs.

Issue #2922: workflow_run followers (Error sink; CI Cost Guard and Stable evidence until
Tue 22 Sep 2026, when each became a scheduled batch pass)
queue on the self-hosted agentbox pool with per-upstream concurrency groups that
never coalesce across superseded main pushes. This sweeper keeps only the trunk
tip and the oldest/newest queued CI push runs, then cancels queued bookkeeping
runs whose head SHA is not retained.

MINI-PRD
    R1 CI retention ............................................... done + regression
       [if] more than two queued CI push runs exist on main
            [then] keep only the oldest and newest by created_at [else stop]
       [if] a queued CI push run is not among the two retained
            [then] cancel it unless dry_run [else stop]
    R2 Bookkeeping sweep .......................................... done + regression
       [if] a queued bookkeeping run's head_sha is not in retained_head_shas
            [then] cancel it and log superseded_by=<trunk_tip> [else stop]
       [if] dry_run is set
            [then] perform zero cancel POSTs [else stop]
    R3 Preconditions .............................................. done + regression
       [if] gh is missing or the API payload is malformed
            [then] exit 10 with a precondition error [else stop]
    R4 Closed-PR sweep ............................................ done + regression
       [if] a queued or running pull_request run's branch has no open PR
            [then] cancel it and log reason=no-open-pr [else stop]
       [if] the open-PR list is empty while PR runs exist
            [then] refuse with a precondition error, never cancel them all [else stop]
       Measured Wed 16 Sep 2026 09:30Z: 13 of 28 unfinished runs were for PRs already
       merged or closed at the SHA under test, holding the 13-slot pytest pool.

USAGE
    uv run --no-sync python -m scripts.ci_trunk_tip_only --dry-run
    uv run --no-sync python -m scripts.ci_trunk_tip_only

-Cursor
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from urllib.parse import quote

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
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.ci_trunk_tip_only") from None
    raise

GATING_WORKFLOW = "CI"
# CI Cost Guard and Stable evidence left this set on Tue 22 Sep 2026: each is a
# scheduled batch pass now, not a per-SHA follower, so there is nothing of them
# to supersede.
BOOKKEEPING_WORKFLOWS = frozenset({"Error sink"})
PAGE_SIZE = 100

# A moving queue is expected (see `_paginated_runs`), so one inconsistent multi-page
# census is not yet a precondition failure: re-read up to this many times, sleeping
# CENSUS_RETRY_SLEEP_SECONDS between attempts, and only fail closed once every
# attempt disagreed.
CENSUS_MAX_ATTEMPTS = 3
CENSUS_RETRY_SLEEP_SECONDS = 1.5

_NOT_YET_QUEUED_CANCEL_MARKERS = ("HTTP 409", "not been queued yet")


class CancelOutcome(Enum):
    """Result of one cancel POST."""

    CANCELLED = "cancelled"
    SKIPPED_NOT_YET_QUEUED = "skipped_not_yet_queued"


@dataclass(frozen=True)
class QueuedRun:
    """One queued workflow run, reduced to the fields the sweep needs."""

    run_id: int
    name: str
    event: str
    head_branch: str
    head_repo_owner: str
    head_sha: str
    created_at: datetime


@dataclass(frozen=True)
class SweepPlan:
    """Runs to cancel and SHAs to retain, before any network side effect."""

    trunk_tip: str
    retained_head_shas: frozenset[str]
    retained_ci_run_ids: frozenset[int]
    ci_to_cancel: tuple[QueuedRun, ...]
    bookkeeping_to_cancel: tuple[QueuedRun, ...]
    bookkeeping_kept: int


@dataclass(frozen=True)
class SweepReport:
    """Counts after a sweep completes."""

    trunk_tip: str
    retained_ci_run_ids: tuple[int, ...]
    ci_cancelled: int
    ci_cancel_skipped_not_yet_queued: int
    bookkeeping_cancelled: int
    bookkeeping_cancel_skipped_not_yet_queued: int
    bookkeeping_kept: int


def _parse_queued_run(raw: object) -> QueuedRun:
    if not isinstance(raw, dict):
        raise PreconditionError(f"queued run was not an object: {raw!r}")
    run_id = raw.get("id")
    name = raw.get("name")
    event = raw.get("event")
    head_branch = raw.get("head_branch")
    head_repo = raw.get("head_repository")
    head_repo_owner = (head_repo or {}).get("owner", {}).get("login")
    head_sha = raw.get("head_sha")
    created_at = raw.get("created_at")
    missing = [
        label
        for label, value in (
            ("id", run_id),
            ("name", name),
            ("event", event),
            ("head_branch", head_branch),
            ("head_repo_owner", head_repo_owner),
            ("head_sha", head_sha),
            ("created_at", created_at),
        )
        if not isinstance(value, (int, str)) or (label == "id" and not isinstance(value, int))
    ]
    if missing or not isinstance(run_id, int) or not isinstance(name, str):
        raise PreconditionError(f"queued run is missing required fields: {raw!r}")
    if (
        not isinstance(event, str)
        or not isinstance(head_branch, str)
        or not isinstance(head_repo_owner, str)
        or not isinstance(head_sha, str)
    ):
        raise PreconditionError(f"queued run has invalid string fields: {raw!r}")
    if not isinstance(created_at, str):
        raise PreconditionError(f"queued run has no created_at: {raw!r}")
    return QueuedRun(
        run_id=run_id,
        name=name,
        event=event,
        head_branch=head_branch,
        head_repo_owner=head_repo_owner,
        head_sha=head_sha,
        created_at=_parse_github_timestamp(created_at, "created_at", run_id),
    )


def _paginated_queued_runs(branch: str) -> list[QueuedRun]:
    return _paginated_runs(f"repos/{REPO}/actions/runs?branch={branch}&status=queued")


def _paginated_runs(
    path: str, fetch_json: Callable[[str], object] | None = None
) -> list[QueuedRun]:
    """Every run the listing serves, paged until a page comes back short.

    GitHub answers total_count from an index that lags the listing: measured live
    at ~3 s for ten minutes on Tue 22 Sep 2026, 41 of 480 responses (8.5%) carried
    a count their own runs did not add up to, and during a merge or cancel burst
    the lag held across consecutive listings (15 over 14 twice; 18 over 17 then
    20 over 15). Reading that as a paging defect made the sweeper red with every
    test job green (main runs 35730977563, 35723785363, 35722075764 and
    35721772426 that day, 4 of its 11 red runs in the last 30), and listing again
    (#3825) did not cure it.

    A SINGLE page shorter than PAGE_SIZE is complete by construction, so it is the
    census and a disagreeing count is only a warning. Across MORE than one page a
    disagreement can also mean the queue moved between page requests, and offset
    paging then skips or repeats a run at the boundary; a skipped CI run whose SHA
    should be retained would let a bookkeeping run sharing that SHA be cancelled.
    A moving queue is expected, so one inconsistent multi-page read is re-read up
    to CENSUS_MAX_ATTEMPTS times (sleeping CENSUS_RETRY_SLEEP_SECONDS between
    attempts) before this stays fail-closed (PreconditionError, exit 10) -- only
    a CONSISTENT read is ever accepted, never a fallback to the disagreeing one.
    `fetch_json` is the GitHub GET; a test hands in captured real payloads keyed by
    the path this asks (no monkeypatching)."""
    fetch = _gh_api_json if fetch_json is None else fetch_json
    attempts = 0
    while True:
        attempts += 1
        runs, total_count, pages = _list_runs_once(path, fetch)
        if len(runs) == total_count:
            return runs
        if pages == 1:
            print(
                f"[WARN] {path} reported total_count={total_count} but its single page holds"
                f" {len(runs)} runs; the count lags the listing, the page is the census",
                file=sys.stderr,
            )
            return runs
        if attempts >= CENSUS_MAX_ATTEMPTS:
            raise PreconditionError(
                f"{path} reported total_count={total_count} but {pages} pages hold"
                f" {len(runs)} runs; the queue moved between page requests on all"
                f" {attempts} attempts, so this census could have skipped a run"
            )
        time.sleep(CENSUS_RETRY_SLEEP_SECONDS)


def _list_runs_once(
    path: str, fetch_json: Callable[[str], object]
) -> tuple[list[QueuedRun], int, int]:
    """The runs, the total_count page 1 reported, and how many pages were read."""
    sep = "&" if "?" in path else "?"
    runs: list[QueuedRun] = []
    total_count: int | None = None
    page = 1
    while True:
        payload = fetch_json(f"{path}{sep}per_page={PAGE_SIZE}&page={page}")
        if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
            raise PreconditionError(f"{path} page {page} has no workflow_runs list: {payload!r}")
        if total_count is None:
            total_count = payload.get("total_count")
            if not isinstance(total_count, int):
                raise PreconditionError(f"{path} response has no integer total_count")
        page_items = payload["workflow_runs"]
        runs.extend(_parse_queued_run(item) for item in page_items)
        if len(page_items) < PAGE_SIZE:
            return runs, total_count, page
        page += 1


def _retained_ci_push_run_ids(ci_push_runs: list[QueuedRun]) -> frozenset[int]:
    """Keep the oldest and newest queued CI push runs; all others are cancelled."""
    if len(ci_push_runs) <= 2:
        return frozenset(run.run_id for run in ci_push_runs)
    ordered = sorted(ci_push_runs, key=lambda run: run.created_at)
    return frozenset({ordered[0].run_id, ordered[-1].run_id})


def build_sweep_plan(trunk_tip: str, queued_runs: list[QueuedRun]) -> SweepPlan:
    """Pure selection logic: which queued runs to cancel and which SHAs to retain."""
    ci_push_runs = [
        run
        for run in queued_runs
        if run.name == GATING_WORKFLOW and run.event == "push"
    ]
    retained_ci_run_ids = _retained_ci_push_run_ids(ci_push_runs)
    retained_ci_shas = frozenset(
        run.head_sha for run in ci_push_runs if run.run_id in retained_ci_run_ids
    )
    retained_head_shas = frozenset({trunk_tip}) | retained_ci_shas

    ci_to_cancel = tuple(
        run for run in ci_push_runs if run.run_id not in retained_ci_run_ids
    )

    bookkeeping_to_cancel: list[QueuedRun] = []
    bookkeeping_kept = 0
    for run in queued_runs:
        if run.name not in BOOKKEEPING_WORKFLOWS:
            continue
        if run.head_sha in retained_head_shas:
            bookkeeping_kept += 1
        else:
            bookkeeping_to_cancel.append(run)

    return SweepPlan(
        trunk_tip=trunk_tip,
        retained_head_shas=retained_head_shas,
        retained_ci_run_ids=retained_ci_run_ids,
        ci_to_cancel=ci_to_cancel,
        bookkeeping_to_cancel=tuple(bookkeeping_to_cancel),
        bookkeeping_kept=bookkeeping_kept,
    )


@dataclass(frozen=True)
class OpenPullRequests:
    """Every open pull request's `(head owner, head ref)`, from a read that COMPLETED.

    Holding one is the claim that the read finished. `_open_pr_heads` raises on a failed or
    malformed page rather than returning a short snapshot, so an EMPTY instance means zero
    open pull requests were MEASURED, not that nothing was measured. A bare frozenset cannot
    carry that difference, which is why the selector used to refuse every empty set: a
    repository whose pull requests are all closed is exactly the state this sweep exists for,
    and the refusal stopped it working there. Zero is a value here as well as an error
    signature, and the type is what tells the two apart.

    The owner is part of the key because forks reuse branch names. Collapsed to the ref
    alone, one open `patch-1` retains every other fork's closed `patch-1` runs, and the queue
    stall this sweep targets persists.
    """

    heads: frozenset[tuple[str, str]]


def closed_pr_runs_to_cancel(
    runs: list[QueuedRun], open_prs: OpenPullRequests
) -> tuple[QueuedRun, ...]:
    """Pull request runs whose `(owner, branch)` has no open pull request."""
    return tuple(
        run
        for run in runs
        if run.event == "pull_request"
        and (run.head_repo_owner, run.head_branch) not in open_prs.heads
    )


def _open_pr_heads() -> OpenPullRequests:
    heads: set[tuple[str, str]] = set()
    page = 1
    while True:
        payload = _gh_api_json(
            f"repos/{REPO}/pulls?state=open&per_page={PAGE_SIZE}&page={page}"
        )
        if not isinstance(payload, list):
            raise PreconditionError(f"open pulls page {page} was not a list: {payload!r}")
        for pull in payload:
            head = pull.get("head") if isinstance(pull, dict) else None
            ref = head.get("ref") if isinstance(head, dict) else None
            repo = head.get("repo") if isinstance(head, dict) else None
            owner = repo.get("owner", {}).get("login") if isinstance(repo, dict) else None
            if not isinstance(ref, str) or not ref:
                raise PreconditionError(f"open pull has no head.ref: {pull!r}")
            if not isinstance(owner, str) or not owner:
                # Refused, not defaulted to this repository's owner: a fork pull request
                # defaulted that way reads as a DIFFERENT pull request, so its live run
                # looks closed and is cancelled.
                raise PreconditionError(f"open pull has no head.repo.owner.login: {pull!r}")
            heads.add((owner, ref))
        if len(payload) < PAGE_SIZE:
            return OpenPullRequests(frozenset(heads))
        page += 1


@dataclass(frozen=True)
class SweepCounts:
    """What the sweep PLANNED to cancel and what it actually cancelled.

    A dry run plans everything and cancels nothing, so one number cannot report both. Read
    from a single `cancelled` count that a dry run incremented, `closed_pr_cancelled=N`
    named something other than what it counted, which is the defect class this lane keeps
    finding elsewhere.
    """

    planned: int
    cancelled: int


def execute_closed_pr_sweep(
    runs: tuple[QueuedRun, ...],
    *,
    dry_run: bool,
    still_closed: Callable[[QueuedRun], bool] = (
        lambda run: _open_pr_count(run.head_repo_owner, run.head_branch) == 0
    ),
) -> SweepCounts:
    """Cancel each selected run, logging one line per run.

    The branch is rechecked immediately before each cancellation. The open-PR snapshot can
    go stale while the sweep runs, and a pull request REOPENED in that window owns a run
    this list still calls closed. Cancelling it breaks the one contract the sweep has.
    """
    planned = 0
    cancelled = 0
    for run in runs:
        if not still_closed(run):
            line = (
                f"closed-pr-skip workflow={run.name} run_id={run.run_id} "
                f"head_branch={run.head_branch} reason=reopened-since-snapshot"
            )
            print(f"::notice::{line}")
            print(line)
            continue
        line = (
            f"closed-pr-cancel workflow={run.name} run_id={run.run_id} "
            f"head_branch={run.head_branch} head_sha={run.head_sha} reason=no-open-pr"
        )
        print(f"::notice::{line}")
        print(line)
        planned += 1
        if not dry_run and _cancel_run(run.run_id) is CancelOutcome.CANCELLED:
            cancelled += 1
    return SweepCounts(planned, cancelled)


def _open_pr_count(head_repo_owner: str, branch: str) -> int:
    """How many open pull requests have this head branch, read fresh.

    The owner comes from the RUN, not from this repository. A pull request opened from a
    fork has a head branch owned by the fork, so asking under this repository's owner
    returns nothing, reads as "no open pull request", and cancels a live run: the one
    thing the reopened-pull-request contract exists to prevent.
    """
    # A branch name may legally contain `&`, `+` or `#`. Interpolated raw, `&` starts a new
    # query parameter and `+` decodes to a space, so the filter asks about a DIFFERENT branch,
    # finds nothing, and the sweep cancels a live run. `:` is left literal because the head
    # filter's own syntax is `owner:branch`.
    head = f"{quote(head_repo_owner, safe='')}:{quote(branch, safe='')}"
    payload = _gh_api_json(f"repos/{REPO}/pulls?state=open&head={head}&per_page=1")
    if not isinstance(payload, list):
        raise PreconditionError(f"open pulls for {branch} was not a list: {payload!r}")
    return len(payload)


def unique_runs(runs: list[QueuedRun]) -> list[QueuedRun]:
    """One entry per run id, first occurrence kept.

    The queued and in-progress snapshots are two requests, and a run that STARTS between
    them appears in both. Concatenated, it is logged twice and cancellation is attempted
    twice, so the reported count measures the race rather than the work.
    """
    seen: dict[int, QueuedRun] = {}
    for run in runs:
        seen.setdefault(run.run_id, run)
    return list(seen.values())


def sweep_closed_pr_runs(*, dry_run: bool) -> SweepCounts:
    """Cancel unfinished pull_request runs for branches with no open pull request.

    Runs are read BEFORE the open-PR list, so any PR that owns a run seen here was
    already open when the list was read.
    """
    runs = unique_runs(
        [
            *_paginated_runs(f"repos/{REPO}/actions/runs?event=pull_request&status=queued"),
            *_paginated_runs(
                f"repos/{REPO}/actions/runs?event=pull_request&status=in_progress"
            ),
        ]
    )
    return execute_closed_pr_sweep(
        closed_pr_runs_to_cancel(runs, _open_pr_heads()), dry_run=dry_run
    )


def _cancel_log_line(run: QueuedRun, trunk_tip: str) -> str:
    return (
        f"bookkeeping-cancel workflow={run.name} run_id={run.run_id} "
        f"head_sha={run.head_sha} superseded_by={trunk_tip}"
    )


def _cancel_skip_log_line(run: QueuedRun, trunk_tip: str) -> str:
    return (
        f"bookkeeping-cancel-skipped workflow={run.name} run_id={run.run_id} "
        f"head_sha={run.head_sha} superseded_by={trunk_tip} reason=not-yet-queued"
    )


def _is_not_yet_queued_cancel_conflict(exc: PreconditionError) -> bool:
    message = str(exc)
    return all(marker in message for marker in _NOT_YET_QUEUED_CANCEL_MARKERS)


def _cancel_run(run_id: int) -> CancelOutcome:
    try:
        _run_gh(["api", "--method", "POST", f"repos/{REPO}/actions/runs/{run_id}/cancel"])
    except PreconditionError as exc:
        if _is_not_yet_queued_cancel_conflict(exc):
            return CancelOutcome.SKIPPED_NOT_YET_QUEUED
        raise
    return CancelOutcome.CANCELLED


def execute_sweep(plan: SweepPlan, *, dry_run: bool) -> SweepReport:
    """Apply cancellations for one plan, emitting structured logs."""
    ci_cancelled = 0
    ci_cancel_skipped_not_yet_queued = 0
    bookkeeping_cancelled = 0
    bookkeeping_cancel_skipped_not_yet_queued = 0

    for run in plan.ci_to_cancel:
        if dry_run:
            ci_cancelled += 1
            continue
        outcome = _cancel_run(run.run_id)
        if outcome is CancelOutcome.CANCELLED:
            ci_cancelled += 1
        elif outcome is CancelOutcome.SKIPPED_NOT_YET_QUEUED:
            ci_cancel_skipped_not_yet_queued += 1

    for run in plan.bookkeeping_to_cancel:
        line = _cancel_log_line(run, plan.trunk_tip)
        print(f"::notice::{line}")
        print(line)
        if dry_run:
            bookkeeping_cancelled += 1
            continue
        outcome = _cancel_run(run.run_id)
        if outcome is CancelOutcome.CANCELLED:
            bookkeeping_cancelled += 1
        elif outcome is CancelOutcome.SKIPPED_NOT_YET_QUEUED:
            bookkeeping_cancel_skipped_not_yet_queued += 1
            skip_line = _cancel_skip_log_line(run, plan.trunk_tip)
            print(f"::notice::{skip_line}")
            print(skip_line)

    return SweepReport(
        trunk_tip=plan.trunk_tip,
        retained_ci_run_ids=tuple(sorted(plan.retained_ci_run_ids)),
        ci_cancelled=ci_cancelled,
        ci_cancel_skipped_not_yet_queued=ci_cancel_skipped_not_yet_queued,
        bookkeeping_cancelled=bookkeeping_cancelled,
        bookkeeping_cancel_skipped_not_yet_queued=bookkeeping_cancel_skipped_not_yet_queued,
        bookkeeping_kept=plan.bookkeeping_kept,
    )


def _default_branch() -> str:
    payload = _gh_api_json(f"repos/{REPO}")
    if not isinstance(payload, dict):
        raise PreconditionError(f"repos/{REPO} response was not an object: {payload!r}")
    branch = payload.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise PreconditionError(f"repos/{REPO} has no default_branch: {payload!r}")
    return branch


def _trunk_tip_sha(branch: str) -> str:
    payload = _gh_api_json(f"repos/{REPO}/commits/{branch}")
    if not isinstance(payload, dict):
        raise PreconditionError(f"commits/{branch} response was not an object: {payload!r}")
    sha = payload.get("sha")
    if not isinstance(sha, str) or not sha:
        raise PreconditionError(f"commits/{branch} has no sha: {payload!r}")
    return sha


def sweep(*, dry_run: bool) -> SweepReport:
    """Resolve trunk tip, plan cancellations, and apply them."""
    branch = _default_branch()
    trunk_tip = _trunk_tip_sha(branch)
    queued_runs = _paginated_queued_runs(branch)
    plan = build_sweep_plan(trunk_tip, queued_runs)
    return execute_sweep(plan, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without POSTing cancellations",
    )
    args = parser.parse_args(argv)
    try:
        report = sweep(dry_run=args.dry_run)
        closed_pr = sweep_closed_pr_runs(dry_run=args.dry_run)
    except PreconditionError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return EXIT_PRECONDITION
    print(
        "[OK] trunk-tip-only sweep: "
        f"trunk_tip={report.trunk_tip} "
        f"retained_ci_run_ids={list(report.retained_ci_run_ids)} "
        f"ci_cancelled={report.ci_cancelled} "
        f"ci_cancel_skipped_not_yet_queued={report.ci_cancel_skipped_not_yet_queued} "
        f"bookkeeping_cancelled={report.bookkeeping_cancelled} "
        f"bookkeeping_cancel_skipped_not_yet_queued="
        f"{report.bookkeeping_cancel_skipped_not_yet_queued} "
        f"bookkeeping_kept={report.bookkeeping_kept} "
        f"closed_pr_planned={closed_pr.planned} closed_pr_cancelled={closed_pr.cancelled}"
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
