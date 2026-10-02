"""Fail-closed CI-wait primitive (issue #1608).

On every poll iteration, also probes the PR's merge/close state via REST
(`repos/.../pulls/{n}`) and stops immediately when the PR has merged
(status MERGED, exit 0) or closed without merging (CLOSED_UNMERGED, exit 5),
instead of polling until timeout while a merged head's check-runs stay stuck
queued (issue #2832).

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

The CONTRACTION gap (issue #1689, PR #1685 thread r3976977757) is the mirror
image: a past push triggered path-filtered workflows (`ci.yml`, `e2e.yml`)
whose job names stayed in `expected` even after a later push made the PR
docs-only, so `poll_until_terminal` waited the full timeout for check-runs
that GitHub would never schedule. `expected_for_head` now intersects the
PR-branch baseline with the job names of workflows whose own `pull_request`
`paths` / `paths-ignore` filters match the PR's cumulative changed-files list
at the current head (GitHub's negation ordering for inclusive `paths`, per
`.github/workflows/ci.yml`), falling back to that applicable set when every
catalogued baseline name is dropped. Expansion (a workflow newly activated by
a path change) is still closed by the two-poll stabilization rule, not by
inflating `expected` from YAML alone -- fail-closed over false SUCCESS.

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
(fleet-af docs/records/nucbox-fleet.md:231-232). Resolving a stale head would let a
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

If `head_sha` is not found in the PR's cached `commits` array at all,
`_previous_shas` treats the WHOLE cached list as baseline candidates rather
than raising (found in review, PR #1685 thread r3976329043): `_head_sha`
now reads the live branch ref, but `_pr_commits` still reads the PR
object's cached `commits` array, which can lag behind it by the same
documented window (fleet-af docs/records/nucbox-fleet.md:231-232). Every commit already
IN that cached list necessarily happened before the live head the cache
has not heard about yet, so treating the lag as an error would turn an
ordinary, brief caching delay into a spurious COULD-NOT-MEASURE right when
a worker most wants an answer.

A baseline is only built from `pull_request`-triggered check-runs, never
every check-run attached to a commit (found in review, PR #1685 thread
r3976329052, and confirmed against this repo's own real data: PR #1681's
merged head carries two check-runs from `create`-triggered runs, not
`pull_request` ones). Including a `create`- or `workflow_dispatch`-only
name in `expected` would make it a permanent, never-satisfiable entry at
every later push on the same PR, since an ordinary `synchronize` push does
not retrigger those events. `_pull_request_triggered_names` resolves each
check-run's Actions run id from its `html_url` and asks that run's own
`event` field, once per DISTINCT run id.

The LIVE poll snapshot is filtered the same way, not just the baseline
(found in review, PR #1685 thread r3976549594): this repo's own
`.github/workflows/ci.yml` documents `workflow_dispatch` as a supplementary
recovery re-run that shares every job name with the `pull_request` run and
is explicitly "not a substitute" for the PR gate. `_latest_by_name`
collapses by name and keeps whichever run is newest, so an un-filtered
snapshot would let a passing dispatch re-run hide a genuinely failed
`pull_request` run behind it -- this primitive's own defect, one layer
further in. `wait_for_checks` filters every poll's snapshot through
`_pull_request_triggered_runs` before collapsing it, using a memoized
`_run_event` wrapper (`_memoized_run_event`) so a run id already resolved
on an earlier poll is never asked about twice.

`_head_sha` resolves the head branch's remote from the PR's ACTUAL head
repository, not always `repo` (found in review, PR #1685 thread
r3976549601): a cross-repository (fork) PR's `headRefName` names a branch
in the contributor's fork. Querying that branch name against `repo` instead
either finds nothing (fails loud, the safe case) or finds an unrelated
same-named branch in the base repo (`main`, for instance) and silently
binds to the wrong commit. `_pr_head_repo`/`_resolve_head_repo` read
`isCrossRepository`/`headRepositoryOwner`/`headRepository` and only fall
back to `repo` when the PR is genuinely same-repository.

Fixtures under `tests/scripts/fixtures/ci_wait/` are checksum-verified
against a `MANIFEST.json` before every test run (found in review, PR #1685
thread r3976549606, pattern follows `tests/fixtures/review_threads/
MANIFEST.json`), so an accidental edit or baseline rewrite to a captured
API response fails loudly instead of silently becoming the authoritative
evidence a test passes against.

The pure reasoning above (baseline derivation, the poll loop, the event and
conclusion filters) lives in `scripts/ci_wait_core.py`, split out once this
module crossed the 600-line file-size ratchet; this module is the `gh`/
`git`/network wiring around it, plus the CLI.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from scripts.ci_wait_core import (
    LifecycleOutcome,
    WaitResult,
    WaitStatus,
    _first_baseline,
    _merge_check_run_pages,
    _previous_shas,
    _pull_request_triggered_runs,
    _resolve_head_repo,
    lifecycle_wait_status,
    poll_until_terminal,
)
from scripts.ci_wait_workflows import (
    WorkflowCatalog,
    default_workflows_dir,
    derive_expected_for_changed_paths,
)
from scripts.review_gh import TriageError, _gh

REPO = "maintainer/music-dj-tools"
DEFAULT_TIMEOUT_S = 1800.0
DEFAULT_POLL_INTERVAL_S = 20.0


# ----- I/O: gh wiring --------------------------------------------------------


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


def _pr_head_repo(pr: str, repo: str = REPO) -> str:
    raw = _gh(
        [
            "pr",
            "view",
            pr,
            "--repo",
            repo,
            "--json",
            "isCrossRepository,headRepositoryOwner,headRepository",
        ]
    )
    return _resolve_head_repo(json.loads(raw), repo)


def _head_sha(pr: str, repo: str = REPO) -> str:
    """The PR's CURRENT head SHA, read from the LIVE branch ref via `git
    ls-remote` rather than `gh pr view --json headRefOid`.

    `headRefOid` is a cached field on the PR object, documented to lag the
    real branch ref right after a push (fleet-af docs/records/nucbox-fleet.md:231-232;
    the same convention `scripts.review_lane._ref_sha` already follows for
    exactly this reason; found in review, PR #1685 thread r3976173905): a
    worker calling this right after its own push could otherwise resolve the
    PREVIOUS head, whose checks are already complete, and report SUCCESS
    while the real new head has nothing registered yet -- reproducing this
    primitive's own defect through the head-resolution step rather than the
    poll loop. The branch NAME has no such lag (only the OID field does), so
    it is read once via `gh pr view` and the live tip is read from the
    remote ref directly.

    The remote is resolved via `_pr_head_repo`, not assumed to be `repo`
    (found in review, PR #1685 thread r3976549601): a cross-repository PR's
    branch lives in the contributor's fork, and `repo` alone would either
    find no such branch (fails loud) or find an unrelated same-named one
    (silently binds to the wrong commit). The URL is spelled out (not
    `origin`) so the answer does not depend on which checkout this happens
    to run from.
    """
    branch = _pr_branch(pr, repo)
    head_repo = _pr_head_repo(pr, repo)
    proc = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{head_repo}.git", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise TriageError(
            f"git ls-remote found no refs/heads/{branch} in {head_repo}: "
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


def _run_event(run_id: str, repo: str = REPO) -> str:
    """The triggering event of Actions run `run_id` (`pull_request`, `push`,
    `create`, `workflow_dispatch`, ...)."""
    raw = _gh(["api", f"repos/{repo}/actions/runs/{run_id}", "-q", ".event"]).strip()
    if not raw:
        raise TriageError(f"gh returned no event for run {run_id} in {repo}")
    return raw


def _memoized_run_event(repo: str = REPO) -> Callable[[str], str]:
    """A `_run_event` wrapper that answers a repeated run id from memory.

    A run's triggering event never changes over its lifetime, but
    `poll_until_terminal` re-fetches the check-run snapshot every
    `poll_interval_s` for up to `timeout_s` -- a bare `_run_event` behind
    that loop would re-ask GitHub about the SAME handful of run ids on every
    single poll (up to ~90 times over the default 30-minute bound) for an
    answer that cannot have changed since the first ask.
    """
    cache: dict[str, str] = {}

    def get(run_id: str) -> str:
        if run_id not in cache:
            cache[run_id] = _run_event(run_id, repo)
        return cache[run_id]

    return get


def _pr_lifecycle(pr: str, repo: str = REPO) -> tuple[str, bool]:
    """The PR object's `state` and `merged` flags from the pulls REST API."""
    raw = _gh(["api", f"repos/{repo}/pulls/{pr}"])
    payload = json.loads(raw)
    state = payload.get("state")
    merged = payload.get("merged")
    if state is None or merged is None:
        raise TriageError(
            f"gh returned incomplete PR lifecycle for PR {pr} in {repo}: {payload!r}"
        )
    return state, bool(merged)


def _pr_lifecycle_message(pr: str, head_sha: str, status: WaitStatus) -> str:
    if status is WaitStatus.MERGED:
        return (
            f"PR #{pr} merged while waiting for CI; stopped without requiring "
            f"terminal check-runs at head {head_sha}"
        )
    if status is WaitStatus.CLOSED_UNMERGED:
        return f"PR #{pr} closed without merge (state=closed, merged=false)"
    raise TriageError(f"no lifecycle message for status {status!r}")


def _pr_changed_files(pr: str, repo: str = REPO) -> list[str]:
    raw = _gh(["pr", "view", pr, "--repo", repo, "--json", "files"])
    files = json.loads(raw).get("files") or []
    paths = [entry["path"] for entry in files if entry.get("path")]
    if not paths:
        raise TriageError(f"gh returned no changed files for PR {pr} in {repo}")
    return paths


def expected_from_previous_head(pr: str, head_sha: str, repo: str = REPO) -> frozenset[str] | None:
    """Expected check-run names for `head_sha`, read from the nearest earlier
    commit on this PR that actually has a `pull_request`-triggered baseline.
    `None` means no baseline could be established (no earlier commit at all,
    or none of them ever got a `pull_request`-triggered check-run) -- the
    caller must report that explicitly, never treat it as "zero checks
    expected".
    """
    candidates = _previous_shas(_pr_commits(pr, repo), head_sha)
    return _first_baseline(
        candidates,
        lambda sha: _check_runs_at_sha(sha, repo),
        lambda run_id: _run_event(run_id, repo),
    )


def expected_for_head(
    pr: str,
    head_sha: str,
    repo: str = REPO,
    *,
    workflows_dir: Path | None = None,
) -> frozenset[str] | None:
    """Expected check-run names at `head_sha`, filtered to workflows that would
    actually run for this PR's cumulative changed-files list at the head."""
    baseline = expected_from_previous_head(pr, head_sha, repo)
    if baseline is None:
        return None
    changed_files = _pr_changed_files(pr, repo)
    catalog = WorkflowCatalog.from_workflows_dir(workflows_dir or default_workflows_dir())
    return derive_expected_for_changed_paths(baseline, changed_files, catalog)


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
    expected = expected_for_head(pr, head_sha, repo)
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

    run_event = _memoized_run_event(repo)

    def lifecycle_check() -> LifecycleOutcome | None:
        state, merged = _pr_lifecycle(pr, repo)
        terminal = lifecycle_wait_status(state, merged)
        if terminal is None:
            return None
        return LifecycleOutcome(terminal, _pr_lifecycle_message(pr, head_sha, terminal))

    start = time.monotonic()
    status, present, message = poll_until_terminal(
        expected,
        lambda: _pull_request_triggered_runs(_check_runs_at_sha(head_sha, repo), run_event),
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
        lifecycle_check=lifecycle_check,
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
        WaitStatus.MERGED: 0,
        WaitStatus.TIMEOUT: 1,
        WaitStatus.NO_BASELINE: 2,
        WaitStatus.FAILURE: 4,
        WaitStatus.CLOSED_UNMERGED: 5,
    }[result.status]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
