"""Pure reasoning for the CI-wait primitive (issue #1608).

Split out from `scripts/ci_wait.py` (round 6 review, PR #1685) once that
module's growth crossed this repo's 600-line file-size ratchet -- a real
refactor along the seam the module's own section dividers already marked
("pure: reasoning over an already-fetched check-run snapshot" / "pure: the
bounded poll loop" vs "I/O: gh wiring"), the same seam
`scripts/trunk_job_verdict_core.py` and `scripts/review_gh.py` (split from
`scripts/review_coverage.py`, issue #1016) already use in this repo, not a
shrink to fit the gate. Everything here takes already-fetched data (or an
injected callable) and returns a decision; nothing here calls `gh`,
`subprocess`, or the network directly. See `scripts/ci_wait.py`'s module
docstring for the full narrative (why this primitive exists, why each guard
below was added, which review thread found each gap).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from scripts.review_gh import TriageError
from scripts.trunk_job_verdict_core import PASSING_JOB_CONCLUSIONS

# `(app slug, check name)` pairs that carry no GitHub Actions run to read an event from.
# Named one by one rather than matched as "anything that is not Actions", and keyed on the
# NAME as well as the app: the wide form removes a check from the observed and expected sets
# together, and a check nobody is waiting for cannot fail. Keyed on the slug alone, every
# other check the same app ever emits disappears with it, so a required one the app adds
# later is silently dropped on the day it appears.
#
# Trunk Merge Queue (issue #4168, adopted Mon 28 Sep 2026) posts its queue marker from the
# `trunk-io` app. The name embeds the target branch, so only main's marker is named here: a
# queue on another branch is a new name a person should classify, not a silent drop. Trunk
# is the only merge queue (ADR-NEW-trunk-is-the-only-merge-queue, Wed 30 Sep 2026), so a
# check-run from the retired queue's app is unrecognized again and raises.
DROPPED_APP_CHECKS = frozenset(
    {
        ("trunk-io", "Trunk Merge Queue (main)"),
    }
)
DROPPED_APP_SLUGS = frozenset(slug for slug, _name in DROPPED_APP_CHECKS)


class WaitStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    TIMEOUT = "TIMEOUT"
    NO_BASELINE = "NO_BASELINE"
    MERGED = "MERGED"
    CLOSED_UNMERGED = "CLOSED_UNMERGED"
    # Only `scripts/ci_watch.py` stops the poll with these, through `inspect_snapshot`.
    GENUINE_FAILURE = "GENUINE_FAILURE"
    HEAD_MOVED = "HEAD_MOVED"


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
        return self.status in {WaitStatus.SUCCESS, WaitStatus.MERGED}


@dataclass(frozen=True)
class LifecycleOutcome:
    status: WaitStatus
    message: str


def lifecycle_wait_status(state: str, merged: bool) -> WaitStatus | None:
    """Map a pulls-API `(state, merged)` pair to a terminal poll outcome.

    `open` PRs keep polling; merged or closed-without-merge stop the loop.
    """
    if merged and state == "closed":
        return WaitStatus.MERGED
    if state == "closed" and not merged:
        return WaitStatus.CLOSED_UNMERGED
    return None


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

    If `head_sha` is not found in `commits` at all, the cached PR object's
    commit history has not caught up to the live branch ref yet (documented
    lag: docs/ops/nucbox-fleet.md:200-202; found in review, PR #1685 thread
    r3976329043): `_head_sha` resolves the CURRENT head from a live `git
    ls-remote`, but `_pr_commits` reads the PR object's cached `commits`
    array, which can be a beat behind it. Every commit already IN that
    cached list necessarily happened before the live head the cache has not
    heard about yet, so the whole cached list -- not none of it -- is a
    valid set of baseline candidates; treating this as an error would turn
    an ordinary, brief caching lag into a spurious COULD-NOT-MEASURE right
    when a worker most wants an answer.
    """
    oids = [commit["oid"] for commit in commits]
    try:
        idx = oids.index(head_sha)
    except ValueError:
        return list(reversed(oids))
    return list(reversed(oids[:idx]))


def _run_id(check_run: dict) -> str:
    """The Actions run id a check-run belongs to, parsed from its `html_url`
    (e.g. `.../actions/runs/34446942870/job/102773687609`). The check-run
    object carries no `event` field of its own -- only `check_suite.id`,
    which does not resolve to a trigger event without a further lookup
    either -- so the run id embedded in the URL is what lets `_run_event`
    ask the run itself what triggered it.
    """
    url = check_run.get("html_url") or ""
    match = re.search(r"/actions/runs/(\d+)/", url)
    if not match:
        raise TriageError(
            f"check-run {check_run.get('id')} has no parseable run id in html_url {url!r}"
        )
    return match.group(1)


def _pull_request_triggered_runs(
    check_runs: Iterable[dict], run_event: Callable[[str], str]
) -> list[dict]:
    """The check-run objects (not merely names) whose Actions run was
    triggered by a `pull_request` event, dropping anything from `push`,
    `create`, `workflow_dispatch`, or any other event a normal PR push will
    not retrigger.

    Used for BOTH halves of this primitive that read check-run sets: the
    baseline (`_pull_request_triggered_names` below, PR #1685 thread
    r3976329052, confirmed against this repo's own real data -- PR #1681's
    merged head carries two check-runs, "macOS native companion wheel" and
    "payload provenance + artifact verification", from `create`-triggered
    runs) AND the live poll snapshot (PR #1685 thread r3976549594): this
    repo's own `.github/workflows/ci.yml` documents its `workflow_dispatch`
    trigger as a SUPPLEMENTARY recovery re-run sharing every job name with
    the `pull_request` run, explicitly "not a substitute" for the PR gate --
    if `poll_until_terminal` collapsed by name without this filter, a
    passing dispatch re-run could hide a genuinely failed `pull_request` run
    behind it, exactly like the bug this whole module exists to close, one
    layer further in. `run_event` is called once per DISTINCT run id, not
    once per check-run, since several check-runs (every pytest shard, for
    example) share one run.
    """
    seen: dict[str, str] = {}
    kept: list[dict] = []
    for run in check_runs:
        # A merge queue's marker (Trunk's) has no Actions run to ask for an event, so
        # it is dropped by APP AND NAME rather than by "not GitHub Actions". Dropping every
        # external app removes
        # a required security, coverage or CI check from the observed AND expected sets at
        # once, which is invisible: the waiter then reports success for a check it stopped
        # looking at. An unrecognized app raises instead, so a person decides.
        app_slug = (run.get("app") or {}).get("slug")
        if (app_slug, run.get("name")) in DROPPED_APP_CHECKS:
            continue
        if app_slug is not None and app_slug != "github-actions":
            raise TriageError(
                f"check-run {run.get('name')!r} comes from unrecognized app {app_slug!r}: "
                "add the (app, name) pair to DROPPED_APP_CHECKS if it carries no Actions "
                "run, or teach this function to wait for it. Dropping it silently would "
                "hide a required check."
            )
        run_id = _run_id(run)
        event = seen.get(run_id)
        if event is None:
            event = run_event(run_id)
            seen[run_id] = event
        if event == "pull_request":
            kept.append(run)
    return kept


def _pull_request_triggered_names(
    check_runs: Iterable[dict], run_event: Callable[[str], str]
) -> frozenset[str]:
    return frozenset(run["name"] for run in _pull_request_triggered_runs(check_runs, run_event))


def _first_baseline(
    candidates: Iterable[str],
    check_runs_at: Callable[[str], list[dict]],
    run_event: Callable[[str], str],
) -> frozenset[str] | None:
    """The pull-request-triggered check-run names of the FIRST candidate SHA
    that actually has any.

    Candidates are prior commits on this PR, nearest first. A commit folded
    into a multi-commit push never got its own `synchronize` event and so
    never has check-runs -- not a baseline of zero, just not a pushed head at
    all -- so it is skipped rather than read as NO_BASELINE prematurely. A
    commit whose only check-runs came from a non-`pull_request` event (a
    manual dispatch, a `create` event) is skipped the same way, for the same
    reason: neither is a real `pull_request`-triggered baseline either.
    """
    for sha in candidates:
        names = _pull_request_triggered_names(check_runs_at(sha), run_event)
        if names:
            return names
    return None


def _resolve_head_repo(payload: dict, repo: str) -> str:
    """Pure: given a `gh pr view --json isCrossRepository,headRepositoryOwner,
    headRepository` payload, decide which `owner/name` the PR's head branch
    actually lives in.

    A cross-repository (fork) PR's `headRefName` names a branch in the
    CONTRIBUTOR's fork, never in `repo` (found in review, PR #1685 thread
    r3976549601): querying that branch name's live ref against `repo`
    instead can silently bind to an unrelated same-named branch in the base
    repo (`main`, for instance) rather than raising, letting this primitive
    report a verdict for the wrong commit entirely. Same-repo PRs
    (`isCrossRepository` false, the common case in this repo) return `repo`
    unchanged.
    """
    if not payload.get("isCrossRepository"):
        return repo
    owner = (payload.get("headRepositoryOwner") or {}).get("login")
    name = (payload.get("headRepository") or {}).get("name")
    if not owner or not name:
        raise TriageError(
            f"PR reported as cross-repository against {repo} but gh returned no head owner/name"
        )
    return f"{owner}/{name}"


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


# ----- pure: the bounded poll loop ------------------------------------------


def _timeout_message(
    elapsed: float, expected: frozenset[str], missing: frozenset[str], latest: dict[str, dict]
) -> str:
    """Why the wait ran out, named precisely. A timeout is a NON-verdict, so the one thing it
    owes the reader is which of the three ways it happened: an expected check never appeared,
    a check never finished, or the set of names never stopped growing."""
    if missing:
        return (
            f"timed out after {elapsed:.0f}s: {len(missing)} of {len(expected)} "
            f"expected check(s) never appeared: {sorted(missing)}"
        )
    pending = sorted(name for name, run in latest.items() if run["status"] != "completed")
    if pending:
        return (
            f"timed out after {elapsed:.0f}s: {len(pending)} check(s) still not terminal: {pending}"
        )
    return (
        f"timed out after {elapsed:.0f}s: all {len(expected)} expected check(s) were "
        "terminal but a new check name appeared on the final poll and never stabilized"
    )


def poll_until_terminal(
    expected: frozenset[str],
    fetch_check_runs: Callable[[], list[dict]],
    *,
    timeout_s: float,
    poll_interval_s: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    lifecycle_check: Callable[[], LifecycleOutcome | None] | None = None,
    inspect_snapshot: Callable[[dict[str, dict]], LifecycleOutcome | None] | None = None,
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

    `inspect_snapshot` sees every snapshot before the terminal check and may stop the poll
    early (the fail-fast watcher stops on the first GENUINE failure). It can only end the
    wait sooner with its own status, never turn a snapshot into SUCCESS.
    """
    start = clock()
    latest: dict[str, dict] = {}
    previous_names: frozenset[str] | None = None
    while True:
        if lifecycle_check is not None:
            outcome = lifecycle_check()
            if outcome is not None:
                return outcome.status, latest, outcome.message
        latest = _latest_by_name(fetch_check_runs())
        if inspect_snapshot is not None:
            outcome = inspect_snapshot(latest)
            if outcome is not None:
                return outcome.status, latest, outcome.message
        missing = _missing(expected, latest)
        observed_names = frozenset(latest.keys())
        stable = observed_names == previous_names
        # `latest and`: zero check-runs is never a verdict. With a non-empty `expected` it is
        # already `missing`; the fail-fast watcher polls a first push with no baseline.
        if latest and not missing and _all_terminal(latest) and stable:
            if not expected and clock() - start < timeout_s:
                # An UNKNOWN lower bound, not an empty one. Without a prior push there is
                # nothing that says which checks this head owes, so "the observed set
                # stopped growing" is not evidence that they all registered: one early
                # check, stable for two polls, would end the watch before the shards exist.
                #
                # This used to hold only an ALL-PASSING snapshot open. Sol's P2 on #3293: a
                # first check finishing known-red or ratchet-debt is not passing, so the
                # snapshot fell straight through to FAILURE and the watch ended on a board
                # that was two checks wide. A snapshot the inspector did NOT stop on is
                # exactly one it has not found a reason to end the wait over, so it is held
                # open too. `inspect_snapshot` still returns on the first GENUINE failure,
                # which is what keeps fail-fast fast; the deadline is only ever reached by
                # failures that are already known not to be the agent's.
                previous_names = observed_names
                sleep(poll_interval_s)
                continue
            if _all_passing(latest):
                if not expected:
                    # The deadline does not convert the missing baseline into one. Reporting
                    # SUCCESS for a head whose owed checks were never established is the one
                    # thing this branch exists to refuse.
                    return (
                        WaitStatus.NO_BASELINE,
                        latest,
                        f"{len(latest)} check(s) passed and the set stopped growing, but no "
                        "previous push established which checks this head owes, so nothing "
                        f"says they all registered; deadline reached after "
                        f"{clock() - start:.0f}s",
                    )
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
            return WaitStatus.TIMEOUT, latest, _timeout_message(elapsed, expected, missing, latest)
        sleep(poll_interval_s)
