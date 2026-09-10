"""Fail-closed CI-wait primitive (issue #1608).

`gh pr checks <n> --watch` is not a "CI is done" predicate: right after a
push, the real Actions check-runs have not registered at the new head yet,
so the command sees only whatever commit STATUSES already exist (bot review
apps that post `success` while doing nothing, per worker-rules Amendment 22)
and returns at once with exit 0. That is not a flake -- a flake is red when
it should be green and gets re-run. This is GREEN FOR AN UNMEASURED SUBJECT,
and every worker in this fleet has been treating that exit code as evidence.

Distinct from #1171: #1171 is the MERGE GATE's after-the-fact view of a head
that never got runs (it now posts a synthetic "PR head CI coverage" failure
status on a delay). This module is the POLLING PRIMITIVE a worker calls right
after its own push, before any such delayed guard could have fired, and it
must never report success for a subject it has not measured.

The fix: bind to the exact head SHA first (never re-resolve mid-wait), derive
what check-runs are EXPECTED at that head from the PR's OWN previous push
(`expected_from_previous_head`), and refuse SUCCESS until every expected name
exists as an Actions check-run AND has status "completed". Commit statuses
(the two lying bots) are never consulted -- only `commits/<sha>/check-runs`,
which is Actions-only and cannot be satisfied by a status app.

Why "previous head on this PR", not "main's HEAD" (the issue's other listed
option): main's HEAD carries `push`-triggered runs, a PR head carries
`pull_request`-triggered runs -- different event, often a different job set
under this repo's path filters. Requiring every name from a `push` run to
reappear at a `pull_request` head would manufacture a false TIMEOUT on a
perfectly healthy PR. A PR's own prior head is event-matched to this one by
construction. It provably cannot be empty when it should not be: a prior
push on this PR has a recorded check-run set by definition of having run CI
before, and if that recorded set is itself empty, that is the Amendment-22 /
#1171 condition repeating and must be reported as NO_BASELINE, never
silently upgraded to a baseline of zero. The one case with no prior push at
all (a brand-new branch's first push) has no PR-branch history to read, and
is reported the same way rather than guessing from an event-mismatched
source.

`expected` is a LOWER BOUND on the current head's real check-run set, not a
claim of completeness: it is read from a PAST push, so a head whose changed
paths trigger MORE workflows than that past push did can have every expected
name present and terminal while a newly triggered run has not registered yet
(found in review, PR #1685 thread r3976054719). `poll_until_terminal` closes
that gap by requiring the observed name set to be IDENTICAL across two
consecutive polls before SUCCESS, not just requiring `expected` to be
satisfied -- a late-registering run changes the observed set and costs one
more poll interval instead of being invisible to it. Residual gaps in the
baseline itself -- a force-push that rewrites away the real previous head
(thread r3976054716), or a brand-new branch's first push -- surface as
NO_BASELINE or as an older-but-real surviving baseline, never as a
fabricated one; both are fail-closed, never a false SUCCESS.

`total_count` from the `check-runs` endpoint is asserted against what this
module actually collected (see `_check_runs_at_sha`): on a genuinely
multi-page result, `gh api --paginate` prints each page as its OWN
concatenated JSON document rather than merging them -- confirmed live by
forcing pagination (`per_page=5`) against a real 13-run head, which printed
three back-to-back `{total_count, check_runs}` objects (5+5+3), not one
merged object. `--slurp` wraps those documents into a single JSON array
instead, so every page's `check_runs` is combined here and the combined
length is asserted against `total_count` (identical on every page).

Baseline lookup walks BACKWARD through this PR's commit history rather than
trusting literal adjacency in `gh pr view --json commits` (found in review,
PR #1685 thread r3975870260): a worker pushing several local commits in one
`git push` advances the head past all of them in a single `synchronize`
event, so the commit immediately before the new head can belong to the SAME
push and carry no check-run history of its own. `expected_from_previous_head`
tries each earlier commit, nearest first, until one has a non-empty
check-run set.

The head SHA itself is read from `git ls-remote` against the live branch
ref, never `gh pr view --json headRefOid` (found in review, PR #1685 thread
r3976173905): `headRefOid` is a cached field on the PR object and is
documented to lag the real branch ref right after a push
(docs/ops/nucbox-fleet.md:200-202). Resolving a stale head would let a
worker calling this right after its own push observe the PREVIOUS head's
already-complete checks and report SUCCESS while the real new head has
nothing registered yet -- reproducing this issue's own defect through head
resolution instead of the poll loop.

A check-run's `status == "completed"` is not "passed": the conclusion can be
`failure`, `timed_out`, `cancelled`, `action_required`, or `stale`, and
treating any of those as SUCCESS would be the same defect this module exists
to close, just moved one field over (found in review, PR #1685 thread
r3976173912). `poll_until_terminal` only returns SUCCESS when every present
run's conclusion is in `scripts.trunk_job_verdict_core.PASSING_JOB_
CONCLUSIONS`; a stable, fully terminal snapshot with any other conclusion
returns FAILURE, a distinct, equally definite result -- never a false green.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from scripts.review_gh import TriageError, _gh
from scripts.trunk_job_verdict_core import PASSING_JOB_CONCLUSIONS

REPO = "maintainer/music-dj-tools"
DEFAULT_TIMEOUT_S = 1800.0
DEFAULT_POLL_INTERVAL_S = 20.0


class WaitStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    TIMEOUT = "TIMEOUT"
    NO_BASELINE = "NO_BASELINE"


@dataclass(frozen=True)
class WaitResult:
    status: WaitStatus
    head_sha: str
    expected: frozenset[str]
    present: dict[str, dict]
    elapsed_s: float
    message: str

    @property
    def ok(self) -> bool:
        return self.status is WaitStatus.SUCCESS


# ----- pure: reasoning over an already-fetched check-run snapshot ----------


def _check_run_names(check_runs: Iterable[dict]) -> frozenset[str]:
    return frozenset(run["name"] for run in check_runs)


def _latest_by_name(check_runs: Iterable[dict]) -> dict[str, dict]:
    """Collapse re-runs to the newest run per name.

    A re-run leaves the OLD completed run in place alongside a NEW one under
    the same name at the same SHA. Keeping the old one would let a stale
    completed run hide a fresh in_progress one behind it -- "old completed +
    new in_progress" must read as NOT terminal. Newest wins by `started_at`,
    `id` as the tiebreaker for equal timestamps.
    """
    latest: dict[str, dict] = {}
    for run in check_runs:
        name = run["name"]
        current = latest.get(name)
        if current is None or (run["started_at"], run["id"]) > (
            current["started_at"],
            current["id"],
        ):
            latest[name] = run
    return latest


def _missing(expected: frozenset[str], latest: dict[str, dict]) -> frozenset[str]:
    return expected - latest.keys()


def _all_terminal(latest: dict[str, dict]) -> bool:
    return all(run["status"] == "completed" for run in latest.values())


def _all_passing(latest: dict[str, dict]) -> bool:
    """True only when every terminal run's conclusion carries no bad news.

    `status == "completed"` alone is not "passed": a check-run can complete
    with conclusion `failure`, `timed_out`, `cancelled`, `action_required`,
    or `stale`, and treating any of those as success would recreate this
    primitive's own defect in a new place -- exactly the illustration named
    in `.claude/rules/verification.md`, "a run conclusion tested for
    not-failure ... cancelled, skipped, timed_out, neutral and stale all
    count as passes" (found in review, PR #1685 thread r3976173912). Reuses
    `scripts.trunk_job_verdict_core`'s PASSING/FAILING conclusion vocabulary
    rather than redefining it, since GitHub check-runs and workflow jobs
    share the same conclusion values.
    """
    return all(run["conclusion"] in PASSING_JOB_CONCLUSIONS for run in latest.values())


def _previous_shas(commits: list[dict], head_sha: str) -> list[str]:
    """Every earlier commit on this PR, NEAREST FIRST, given `gh pr view
    --json commits` (oldest first, per gh's own ordering, verified live).
    Empty on a single-commit PR: there is no prior commit on this branch at
    all, which the caller must report as NO_BASELINE rather than inventing
    one from an event-mismatched source.
    """
    oids = [commit["oid"] for commit in commits]
    try:
        idx = oids.index(head_sha)
    except ValueError:
        raise ValueError(
            f"head {head_sha} is not in its own PR's commit list "
            f"(most recent: {oids[-1] if oids else '<none>'})"
        ) from None
    return list(reversed(oids[:idx]))


def _first_baseline(
    candidates: Iterable[str], check_runs_at: Callable[[str], list[dict]]
) -> frozenset[str] | None:
    """The check-run names of the FIRST candidate SHA that actually has any.

    Candidates are prior commits on this PR, nearest first. A commit folded
    into a multi-commit push never got its own `synchronize` event and so
    never has check-runs -- not a baseline of zero, just not a pushed head at
    all -- so it is skipped rather than read as NO_BASELINE prematurely.
    """
    for sha in candidates:
        names = _check_run_names(check_runs_at(sha))
        if names:
            return names
    return None


# ----- pure: the bounded poll loop ------------------------------------------


def poll_until_terminal(
    expected: frozenset[str],
    fetch_check_runs: Callable[[], list[dict]],
    *,
    timeout_s: float,
    poll_interval_s: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[WaitStatus, dict[str, dict], str]:
    """Poll `fetch_check_runs` until every name in `expected` exists AND is
    terminal, or `timeout_s` elapses. Never returns SUCCESS for a snapshot
    missing an expected name, however quickly that snapshot arrived -- the
    exact defect this primitive replaces let a same-second read of zero
    runs satisfy it. Never returns SUCCESS for a snapshot that is complete
    and stable but did not actually pass, either: `status == "completed"`
    is not "passed" (found in review, PR #1685 thread r3976173912), so a
    fully terminal, stable snapshot with any non-passing conclusion returns
    FAILURE, not SUCCESS -- see `_all_passing`.

    `expected` is a LOWER BOUND read from a prior push, not a guarantee of
    completeness: a head whose changed paths trigger MORE workflows than the
    baseline push (found in review, PR #1685 thread r3976054719) can have
    every `expected` name present and terminal while a newly triggered run
    has not registered yet under a name `expected` never knew to look for.
    `_all_terminal` already inspects every OBSERVED run, not just the
    expected ones, so a late run that HAS registered already blocks success;
    the gap is the window before it registers at all. Closing that gap needs
    no knowledge of what name to expect -- only proof that the observed name
    set has stopped growing: SUCCESS requires the same set of observed names
    on two consecutive polls, so a run that registers between polls delays
    success by exactly one more interval instead of being invisible to it.
    """
    start = clock()
    latest: dict[str, dict] = {}
    previous_names: frozenset[str] | None = None
    while True:
        latest = _latest_by_name(fetch_check_runs())
        missing = _missing(expected, latest)
        observed_names = frozenset(latest.keys())
        stable = observed_names == previous_names
        if not missing and _all_terminal(latest) and stable:
            if _all_passing(latest):
                return (
                    WaitStatus.SUCCESS,
                    latest,
                    f"all {len(expected)} expected check(s) present and terminal, "
                    "and no new check appeared on the following poll",
                )
            failing = sorted(
                f"{name} ({run['conclusion']})"
                for name, run in latest.items()
                if run["conclusion"] not in PASSING_JOB_CONCLUSIONS
            )
            return (
                WaitStatus.FAILURE,
                latest,
                f"all check(s) present and terminal, but {len(failing)} did not pass: {failing}",
            )
        previous_names = observed_names
        elapsed = clock() - start
        if elapsed >= timeout_s:
            if missing:
                message = (
                    f"timed out after {elapsed:.0f}s: {len(missing)} of "
                    f"{len(expected)} expected check(s) never appeared: "
                    f"{sorted(missing)}"
                )
            else:
                pending = sorted(
                    name for name, run in latest.items() if run["status"] != "completed"
                )
                if pending:
                    message = (
                        f"timed out after {elapsed:.0f}s: {len(pending)} check(s) "
                        f"still not terminal: {pending}"
                    )
                else:
                    message = (
                        f"timed out after {elapsed:.0f}s: all {len(expected)} expected "
                        "check(s) were terminal but a new check name appeared on the "
                        "final poll and never stabilized"
                    )
            return WaitStatus.TIMEOUT, latest, message
        sleep(poll_interval_s)


# ----- I/O: gh wiring --------------------------------------------------------


def _merge_check_run_pages(pages: list[dict]) -> list[dict]:
    """Flatten `gh api --paginate --slurp`'s pages of `{total_count,
    check_runs}` into one list of check-run objects.

    Kept pure (mirrors `scripts.review_gh._flatten_pages`, issue #1016's own
    fix for the bare-array shape) so a genuine multi-page response -- proven
    live by forcing `per_page=5` against a real 13-run head, which back then
    printed three separate `{total_count, check_runs}` documents (5+5+3), not
    one merged object -- is directly testable on that recorded 3-page fixture
    without a network call. The combined length is asserted against
    `total_count` (identical on every page) so an incomplete merge fails loud
    instead of silently under-reporting.
    """
    runs = [run for page in pages for run in page["check_runs"]]
    total_count = pages[0]["total_count"] if pages else 0
    if len(runs) != total_count:
        raise TriageError(
            f"check-runs: total_count={total_count} but collected "
            f"{len(runs)} check-run object(s) across {len(pages)} page(s) -- "
            "incomplete page merge, not a real count"
        )
    return runs


def _check_runs_at_sha(sha: str, repo: str = REPO) -> list[dict]:
    """All Actions check-runs at `sha`, every page merged by
    `_merge_check_run_pages` (see the module docstring for why `--slurp` is
    required for this object-wrapped endpoint shape)."""
    raw = _gh(["api", f"repos/{repo}/commits/{sha}/check-runs", "--paginate", "--slurp"])
    return _merge_check_run_pages(json.loads(raw))


def _pr_branch(pr: str, repo: str = REPO) -> str:
    """The PR's head BRANCH NAME, in the EXPLICITLY named `repo` -- never the
    cwd-inferred repo `scripts.review_gh._head_sha` uses, so a caller naming
    a non-default `--repo` cannot have this half silently look at the wrong
    repository while `_check_runs_at_sha` correctly looks at the named one
    (found in review, PR #1685 thread r3975870266).
    """
    raw = _gh(
        ["pr", "view", pr, "--repo", repo, "--json", "headRefName", "-q", ".headRefName"]
    ).strip()
    if not raw:
        raise TriageError(f"gh returned no headRefName for PR {pr} in {repo}")
    return raw


def _head_sha(pr: str, repo: str = REPO) -> str:
    """The PR's CURRENT head SHA, read from the LIVE branch ref via `git
    ls-remote` rather than `gh pr view --json headRefOid`.

    `headRefOid` is a cached field on the PR object, documented to lag the
    real branch ref right after a push (docs/ops/nucbox-fleet.md:200-202;
    the same convention `scripts.review_lane._ref_sha` already follows for
    exactly this reason; found in review, PR #1685 thread r3976173905): a
    worker calling this right after its own push could otherwise resolve the
    PREVIOUS head, whose checks are already complete, and report SUCCESS
    while the real new head has nothing registered yet -- reproducing this
    primitive's own defect through the head-resolution step rather than the
    poll loop. The branch NAME has no such lag (only the OID field does), so
    it is read once via `gh pr view` and the live tip is read from the
    remote ref directly. The remote URL is spelled out (not `origin`) so the
    answer does not depend on which checkout this happens to run from.
    """
    branch = _pr_branch(pr, repo)
    proc = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{repo}.git", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise TriageError(
            f"git ls-remote found no refs/heads/{branch} in {repo}: "
            f"{proc.stderr.strip() or '<empty>'}"
        )
    return proc.stdout.split()[0]


def _pr_commits(pr: str, repo: str = REPO) -> list[dict]:
    raw = _gh(["pr", "view", pr, "--repo", repo, "--json", "commits"])
    payload = json.loads(raw)
    commits = payload.get("commits") or []
    if not commits:
        raise TriageError(f"gh returned no commits for PR {pr} in {repo}")
    return commits


def expected_from_previous_head(pr: str, head_sha: str, repo: str = REPO) -> frozenset[str] | None:
    """Expected check-run names for `head_sha`, read from the nearest earlier
    commit on this PR that actually has any. `None` means no baseline could
    be established (no earlier commit at all, or none of them ever got a
    check-run) -- the caller must report that explicitly, never treat it as
    "zero checks expected".
    """
    try:
        candidates = _previous_shas(_pr_commits(pr, repo), head_sha)
    except ValueError as exc:
        raise TriageError(f"PR #{pr}: {exc}") from None
    return _first_baseline(candidates, lambda sha: _check_runs_at_sha(sha, repo))


def wait_for_checks(
    pr: str,
    repo: str = REPO,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> WaitResult:
    """Resolve PR#`pr`'s CURRENT head once (from the live branch ref, not the
    PR object's lagging `headRefOid`), bind everything below to it, and
    refuse SUCCESS until every expected Actions check-run at that exact SHA
    exists, is completed, and passed."""
    head_sha = _head_sha(pr, repo)
    expected = expected_from_previous_head(pr, head_sha, repo)
    if expected is None:
        return WaitResult(
            WaitStatus.NO_BASELINE,
            head_sha,
            frozenset(),
            {},
            0.0,
            "no earlier commit on this PR has a non-empty check-run baseline; "
            "cannot tell what should run at this head -- this is a measurement "
            "gap, not evidence of success",
        )

    start = time.monotonic()
    status, present, message = poll_until_terminal(
        expected,
        lambda: _check_runs_at_sha(head_sha, repo),
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
    )
    return WaitResult(status, head_sha, expected, present, time.monotonic() - start, message)


# ----- CLI -------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pr")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--poll-interval-s", type=float, default=DEFAULT_POLL_INTERVAL_S)
    args = parser.parse_args(argv[1:])

    try:
        result = wait_for_checks(
            args.pr,
            args.repo,
            timeout_s=args.timeout_s,
            poll_interval_s=args.poll_interval_s,
        )
    except TriageError as exc:
        print(f"[ci-wait] COULD NOT MEASURE: {exc}", file=sys.stderr)
        return 3

    print(f"[ci-wait] PR #{args.pr} @ head {result.head_sha}: {result.status.value}")
    print(f"  {result.message}")
    for name in sorted(result.expected):
        run = result.present.get(name)
        state = f"{run['status']}/{run['conclusion']}" if run else "MISSING"
        print(f"    {name}: {state}")
    return {
        WaitStatus.SUCCESS: 0,
        WaitStatus.TIMEOUT: 1,
        WaitStatus.NO_BASELINE: 2,
        WaitStatus.FAILURE: 4,
    }[result.status]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
