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

`total_count` from the `check-runs` endpoint is asserted against what this
module actually collected (see `_check_runs_at_sha`): `gh api --paginate` on
this endpoint merges every page into one printed object rather than one
object per page (verified live, gh 2.98.0), so a naive parse of only the
first chunk would silently under-count on a SHA with 30+ check-runs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from scripts.review_gh import TriageError, _gh, _head_sha

REPO = "maintainer/music-dj-tools"
DEFAULT_TIMEOUT_S = 1800.0
DEFAULT_POLL_INTERVAL_S = 20.0


class WaitStatus(StrEnum):
    SUCCESS = "SUCCESS"
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


def _previous_head_sha(commits: list[dict], head_sha: str) -> str | None:
    """The SHA of this PR's own previous push, given `gh pr view --json
    commits` (oldest first, per gh's own ordering, verified live). `None` on
    a single-commit PR: there is no prior push on this branch to read a
    baseline from, which the caller must report as NO_BASELINE rather than
    inventing one from an event-mismatched source.
    """
    oids = [commit["oid"] for commit in commits]
    try:
        idx = oids.index(head_sha)
    except ValueError:
        raise ValueError(
            f"head {head_sha} is not in its own PR's commit list "
            f"(most recent: {oids[-1] if oids else '<none>'})"
        ) from None
    return oids[idx - 1] if idx > 0 else None


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
    runs satisfy it.
    """
    start = clock()
    latest: dict[str, dict] = {}
    while True:
        latest = _latest_by_name(fetch_check_runs())
        missing = _missing(expected, latest)
        if not missing and _all_terminal(latest):
            return (
                WaitStatus.SUCCESS,
                latest,
                f"all {len(expected)} expected check(s) present and terminal",
            )
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
                message = (
                    f"timed out after {elapsed:.0f}s: {len(pending)} check(s) "
                    f"still not terminal: {pending}"
                )
            return WaitStatus.TIMEOUT, latest, message
        sleep(poll_interval_s)


# ----- I/O: gh wiring --------------------------------------------------------


def _check_runs_at_sha(sha: str, repo: str = REPO) -> list[dict]:
    """All Actions check-runs at `sha`, every page.

    The `check-runs` endpoint wraps its array in `{total_count, check_runs}`
    rather than returning a bare list, so `gh api --paginate` merges pages
    into one printed OBJECT rather than one array per page (confirmed live,
    gh 2.98.0, against a 13-run head). `total_count` is asserted against what
    was actually collected so an incomplete merge fails loud instead of
    silently under-reporting -- the same trap `scripts.review_gh
    ._paginated_json_list` closes for bare-array endpoints, here for the
    object-wrapped shape.
    """
    raw = _gh(["api", f"repos/{repo}/commits/{sha}/check-runs", "--paginate"])
    payload = json.loads(raw)
    runs = payload["check_runs"]
    if len(runs) != payload["total_count"]:
        raise TriageError(
            f"check-runs at {sha}: total_count={payload['total_count']} but "
            f"collected {len(runs)} check-run object(s) -- incomplete page "
            "merge, not a real count"
        )
    return runs


def _pr_commits(pr: str) -> list[dict]:
    raw = _gh(["pr", "view", pr, "--json", "commits"])
    payload = json.loads(raw)
    commits = payload.get("commits") or []
    if not commits:
        raise TriageError(f"gh returned no commits for PR {pr}")
    return commits


def expected_from_previous_head(pr: str, head_sha: str, repo: str = REPO) -> frozenset[str] | None:
    """Expected check-run names for `head_sha`, read from this PR's own
    previous push. `None` means no baseline could be established (no prior
    push, or the prior push itself recorded zero check-runs) -- the caller
    must report that explicitly, never treat it as "zero checks expected".
    """
    try:
        prev_sha = _previous_head_sha(_pr_commits(pr), head_sha)
    except ValueError as exc:
        raise TriageError(f"PR #{pr}: {exc}") from None
    if prev_sha is None:
        return None
    names = _check_run_names(_check_runs_at_sha(prev_sha, repo))
    return names or None


def wait_for_checks(
    pr: str,
    repo: str = REPO,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> WaitResult:
    """Resolve PR#`pr`'s CURRENT head once, bind everything below to it, and
    refuse SUCCESS until every expected Actions check-run at that exact SHA
    exists and is completed."""
    head_sha = _head_sha(pr)
    expected = expected_from_previous_head(pr, head_sha, repo)
    if expected is None:
        return WaitResult(
            WaitStatus.NO_BASELINE,
            head_sha,
            frozenset(),
            {},
            0.0,
            "no non-empty check-run baseline on this PR's previous push; "
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
    }[result.status]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
