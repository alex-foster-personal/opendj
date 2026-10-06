"""Fail-fast CI watcher for a pull request: stream new failures, stop on the first GENUINE one.

`just ci-watch <PR>` is the merging agent's single wait (spec: specs/ci-fail-fast.md, parts 1
and 2, SMARTEST-CI round 3). Measured Wed 16 Sep 2026 over 30 failing PR runs: the first
failed job landed at a median 4.4 min, the last job at 211 min, and agents waited for the
last. This polls the pinned head every 30 s, prints each newly finished non-passing check as
a blob (verdict, job, the identities that decided it), and exits as soon as one is GENUINE.

GENUINE means failing here and not on main and not a known flake, with the fleet merge
gate's exact rules (`scripts/ci_failure_ids.py`, `scripts/ci_main_red.py`), so a failure the
watcher stops on is one the gate would refuse.

Completion reuses `scripts/ci_wait`: expected checks come from this PR's previous push, and
success needs every one present, terminal, and a stable check set across two polls.

Exit codes (2 is argparse's usage error, so no verdict uses it):
    0 GREEN            every check passed at the pinned head, or the PR merged meanwhile
    1 GENUINE          a failure not on main: stop waiting and fix it
    3 UNKNOWN          could not measure: head moved, timeout, no check runs, a cancelled or
                       infra-killed job, no baseline to prove completeness, or an API error
    4 KNOWN_RED_ONLY   finished, and every failure is main's own red, a known flake, or
                       ratchet debt (mergeable under SHIP mode, never GREEN)
    5 CLOSED           the PR closed without merging

MINI-PRD
    R1 Stream failures ................................................ done + regression
       [if] a check finishes non-passing [then] one blob line names its verdict [else stop]
       [if] a check was already reported [then] it is not reported again [else stop]
    R2 Stop early ..................................................... done + regression
       [if] a finished check is GENUINE [then] exit 1 on that poll [else stop]
       [if] a finished check is only main's red [then] keep waiting [else stop]
    R3 Never a verdict from absence ................................... done + regression
       [if] the head moves [then] exit 3 [else stop]
       [if] a job was cancelled or died with no identity [then] the end is 3, not 0 or 4 [else stop]
       [if] no baseline exists [then] a clean finish is 3, not 0 [else stop]

Usage:
    just ci-watch 3288
    uv run --no-sync python -m scripts.ci_watch 3288 --timeout-s 7200
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum

from scripts.ci_failure_ids import (
    JobClassification,
    JobVerdict,
    classify_job,
    error_excerpt,
    failed_identities,
    failure_beyond_tests,
    ratchet_breach,
    residual_identities,
    unpartitionable_exit,
)
from scripts.ci_main_red import (
    LogUnreadable,
    MainRed,
    cached_main_red,
    read_job_log,
    stale_red_identities,
)
from scripts.ci_wait import (
    _check_runs_at_sha,
    _head_sha,
    _memoized_run_event,
    _pr_lifecycle,
    _pr_lifecycle_message,
    expected_for_head,
)
from scripts.ci_wait_core import (
    LifecycleOutcome,
    WaitStatus,
    _pull_request_triggered_runs,
    lifecycle_wait_status,
    poll_until_terminal,
)
from scripts.review_coverage_carry import fetch_commits, is_ancestor
from scripts.review_gate_freshness import CHECKOUT_ROOT
from scripts.review_gh import TriageError
from scripts.trunk_job_verdict_core import PASSING_JOB_CONCLUSIONS

REPO = "private_owner/music-dj-tools"
POLL_INTERVAL_S = 30.0
TIMEOUT_S = 4 * 3600.0
NO_RUNS_GRACE_S = 600.0
HEARTBEAT_S = 300.0
CLASSIFIED_CONCLUSIONS = frozenset({"failure", "timed_out"})
IDENTITIES_SHOWN = 8


class Exit(IntEnum):
    GREEN = 0
    GENUINE = 1
    UNKNOWN = 3
    KNOWN_RED_ONLY = 4
    CLOSED = 5


def _advanced_past(measured_sha: str, pr_head_sha: str) -> bool:
    """True when the pull request's history already contains `measured_sha` (issue #3344):
    its merge base is at or after that commit, so a baseline measured there cannot say main
    is still red on it for THIS pull request. Fetches the shas by name first -- never via
    FETCH_HEAD, which concurrent agents in this shared checkout overwrite (CLAUDE.md).

    An unmeasurable ancestry (a git failure, a missing object `fetch_commits` could not
    resolve) comes back True: undeterminable must not preserve the excuse, the same
    direction `stale_red_identities` documents for its own seam.
    """
    try:
        fetch_commits(CHECKOUT_ROOT, measured_sha, pr_head_sha)
        return is_ancestor(CHECKOUT_ROOT, measured_sha, pr_head_sha)
    except TriageError:
        return True


@dataclass
class FailureWatch:
    """Classifies each newly finished non-passing check once, and says when to stop."""

    log_of: Callable[[int], str]
    main_red: Callable[[], MainRed]
    emit: Callable[[str], None]
    clock: Callable[[], float] = time.monotonic
    pr_head_sha: str = ""
    advanced_past: Callable[[str, str], bool] = _advanced_past
    verdicts: dict[int, JobClassification | None] = field(default_factory=dict)
    excerpts: dict[int, list[str]] = field(default_factory=dict)
    started: float | None = None
    announced_baseline: bool = False
    last_heartbeat: float = 0.0

    def inspect(self, latest: dict[str, dict]) -> LifecycleOutcome | None:
        now = self.clock()
        self.started = now if self.started is None else self.started
        fresh = [
            run
            for _, run in sorted(latest.items())
            if run["status"] == "completed"
            and run["conclusion"] not in PASSING_JOB_CONCLUSIONS
            and run["id"] not in self.verdicts
        ]
        if fresh:
            self.emit(f"[ci-watch {_utc()}] {len(fresh)} newly finished non-passing check(s)")
        genuine: list[str] = []
        for run in fresh:
            classification = self._classify(run)
            self.verdicts[run["id"]] = classification
            self.emit(_blob_line(run, classification, self.excerpts.get(run["id"], [])))
            if classification is not None and classification.verdict is JobVerdict.GENUINE:
                genuine.append(run["name"])
        if genuine:
            return LifecycleOutcome(
                WaitStatus.GENUINE_FAILURE, f"GENUINE failure in {len(genuine)} check(s): {genuine}"
            )
        if not latest and now - self.started >= NO_RUNS_GRACE_S:
            return LifecycleOutcome(
                WaitStatus.TIMEOUT, f"no check runs registered within {NO_RUNS_GRACE_S:.0f}s"
            )
        if now - self.last_heartbeat >= HEARTBEAT_S:
            self.last_heartbeat = now
            done = sum(run["status"] == "completed" for run in latest.values())
            self.emit(f"[ci-watch {_utc()}] waiting: {done} of {len(latest)} check(s) finished")
        return None

    def _classify(self, run: dict) -> JobClassification | None:
        """None means UNMEASURED: cancelled, stale, action_required, no job, or no log."""
        job_id = _job_id(run)
        if run["conclusion"] not in CLASSIFIED_CONCLUSIONS or job_id is None:
            return None
        red = self.main_red()
        if not self.announced_baseline:
            self.announced_baseline = True
            measured = red.measured_sha or red.main_sha
            at = (
                f"{red.main_sha[:9]}"
                if measured == red.main_sha
                else f"{measured[:9]}, main head {red.main_sha[:9]}"
            )
            self.emit(f"  main red baseline: {len(red.identities)} identity(ies) at {at}")
        try:
            log = self.log_of(job_id)
        except LogUnreadable as unreadable:
            self.emit(f"  (log unreadable) {unreadable}")
            return None
        identities = failed_identities(log)
        # The CONCLUSION is evidence in its own right. A timed-out job's log is truncated
        # wherever the clock stopped, so the runner's own terminal line may never have been
        # written; matching on log text alone would read a truncated log full of known-red
        # identities as KNOWN_RED and exit mergeable despite the timeout.
        beyond = (
            failure_beyond_tests(log)
            or (
                ["exit code 1 in a log that could not be partitioned into steps"]
                if unpartitionable_exit(log)
                else []
            )
            or (
                [f"the job conclusion is {run['conclusion']}"]
                if run["conclusion"] != "failure"
                else []
            )
        )
        if not identities:
            self.excerpts[run["id"]] = error_excerpt(log)
        elif beyond:
            self.excerpts[run["id"]] = beyond
        return classify_job(
            run["name"],
            identities,
            red.identities,
            red.failed_job_names,
            red.unreadable_job_names,
            beyond_tests=bool(beyond),
            ratchet_breach_seen=ratchet_breach(log),
            baseline_stale=self._baseline_stale(identities, red),
        )

    def _baseline_stale(self, identities: frozenset[str], red: MainRed) -> bool:
        """True when this job is excused ONLY by baseline identities THIS pull request has
        already advanced past (issue #3344): main could have fixed them since, so
        subtracting them is unproven, not merely old. Bounded to this job's own identities,
        not the whole baseline, and only checked once the job would otherwise be KNOWN_RED --
        the ancestor check is real git I/O and most jobs never reach it.
        """
        if not identities or residual_identities(identities, red.identities):
            return False
        stale = stale_red_identities(red.identities_by_sha, self.pr_head_sha, self.advanced_past)
        return bool(identities & stale)


def _job_id(run: dict) -> int | None:
    match = re.search(r"/actions/runs/\d+/job/(\d+)", run.get("html_url") or "")
    return int(match.group(1)) if match else None


def _utc() -> str:
    return datetime.now(UTC).strftime("%H:%M:%SZ")


def _blob_line(run: dict, classification: JobClassification | None, excerpt: list[str]) -> str:
    if classification is None:
        return f"  UNMEASURED {run['name']} ({run['conclusion']}) {run.get('html_url', '')}"
    decisive = classification.residual or classification.identities
    shown = sorted(decisive)[:IDENTITIES_SHOWN]
    more = f" (+{len(decisive) - len(shown)} more)" if len(decisive) > len(shown) else ""
    if not decisive:
        shown = [f"(no test identity) {line}" for line in excerpt] or [
            "(no test identity and no error line found: read the log)"
        ]
    if classification.verdict is JobVerdict.UNEXPLAINED:
        # The identities are not why this is unmeasured, so leading with them would hide the
        # reason an operator has to act on.
        shown = [f"(beyond the tests) {line}" for line in excerpt] + shown
    body = "".join(f"\n    {identity}" for identity in shown)
    return f"  {classification.verdict} {run['name']} ({run['conclusion']}){more}{body}"


_STATUS_EXITS: dict[WaitStatus, tuple[Exit, str]] = {
    WaitStatus.GENUINE_FAILURE: (Exit.GENUINE, "stop waiting: fix the GENUINE failure above"),
    WaitStatus.CLOSED_UNMERGED: (Exit.CLOSED, "the PR closed without merging"),
    WaitStatus.MERGED: (Exit.GREEN, "the PR merged while watching"),
    WaitStatus.TIMEOUT: (Exit.UNKNOWN, "TIMEOUT: nothing was proven"),
    WaitStatus.NO_BASELINE: (Exit.UNKNOWN, "NO_BASELINE: nothing was proven"),
    WaitStatus.HEAD_MOVED: (Exit.UNKNOWN, "HEAD_MOVED: this head is no longer the PR"),
}


def exit_for(status: WaitStatus, watch: FailureWatch, *, has_baseline: bool) -> tuple[Exit, str]:
    """Map the poll's end to an exit code. Pure."""
    if status in _STATUS_EXITS:
        return _STATUS_EXITS[status]
    verdicts = list(watch.verdicts.values())
    if any(v is not None and v.verdict is JobVerdict.GENUINE for v in verdicts):
        return Exit.GENUINE, "a GENUINE failure was recorded"
    unmeasured = [
        v
        for v in verdicts
        if v is None
        or v.verdict
        in (
            JobVerdict.INFRA,
            JobVerdict.BASELINE_UNREADABLE,
            JobVerdict.BASELINE_MISMATCH,
            JobVerdict.BASELINE_STALE,
            JobVerdict.UNEXPLAINED,
        )
    ]
    if unmeasured:
        return Exit.UNKNOWN, f"{len(unmeasured)} check(s) finished without a measurement"
    if not has_baseline:
        return Exit.UNKNOWN, "no earlier push on this PR to prove every expected check ran"
    if status is WaitStatus.SUCCESS:
        return Exit.GREEN, "every expected check passed"
    if status is WaitStatus.FAILURE and verdicts:
        return Exit.KNOWN_RED_ONLY, "finished; every failure is main's red, a flake, or debt"
    return Exit.UNKNOWN, f"unmapped end state {status}"


def watch_pr(pr: str, *, timeout_s: float, poll_interval_s: float) -> Exit:
    head = _head_sha(pr, REPO)
    expected = expected_for_head(pr, head, REPO)
    print(f"[ci-watch {_utc()}] PR #{pr} pinned head {head}; polling every {poll_interval_s:.0f}s")
    if expected is None:
        print("  no baseline from an earlier push: failures are watched, GREEN cannot be proven")
    watch = FailureWatch(read_job_log, cached_main_red, print, pr_head_sha=head)
    run_event = _memoized_run_event(REPO)

    def lifecycle() -> LifecycleOutcome | None:
        state, merged = _pr_lifecycle(pr, REPO)
        terminal = lifecycle_wait_status(state, merged)
        if terminal is not None:
            return LifecycleOutcome(terminal, _pr_lifecycle_message(pr, head, terminal))
        moved = _head_sha(pr, REPO)
        if moved != head:
            return LifecycleOutcome(WaitStatus.HEAD_MOVED, f"head moved {head[:9]} -> {moved[:9]}")
        return None

    status, _, message = poll_until_terminal(
        expected or frozenset(),
        lambda: _pull_request_triggered_runs(_check_runs_at_sha(head, REPO), run_event),
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
        lifecycle_check=lifecycle,
        inspect_snapshot=watch.inspect,
    )
    code, meaning = exit_for(status, watch, has_baseline=expected is not None)
    print(f"[ci-watch {_utc()}] {code.name} (exit {int(code)}): {meaning}. {message}")
    return code


def main(argv: list[str] | None = None, watch: Callable[..., Exit] = watch_pr) -> int:
    """`watch` is a declared seam, the same shape as `log_of` and `main_red` above, so the
    crash path can be exercised without replacing module state."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pr")
    parser.add_argument("--timeout-s", type=float, default=TIMEOUT_S)
    parser.add_argument("--poll-interval-s", type=float, default=POLL_INTERVAL_S)
    args = parser.parse_args(argv)
    try:
        return int(
            watch(args.pr, timeout_s=args.timeout_s, poll_interval_s=args.poll_interval_s)
        )
    except Exception:  # an uncaught crash exits 1, which would read as GENUINE
        traceback.print_exc()
        print(
            f"[ci-watch {_utc()}] UNKNOWN (exit 3): the watcher could not measure", file=sys.stderr
        )
        return int(Exit.UNKNOWN)


if __name__ == "__main__":
    raise SystemExit(main())
