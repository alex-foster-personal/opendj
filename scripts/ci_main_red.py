"""What is red on main right now: failing test identities and failed job names.

In-repo port of nucbox ~/jobs/sweep/main-failing-tests.sh so `scripts/ci_watch.py` and the
fleet merge gate subtract the same baseline. A pull request that fails the same tests as main
is not the pull request's fault; trunk repair owns them.

Main's red is the UNION over the newest `MAIN_WINDOW` verdicts of `ci.yml` and `e2e.yml` on
main, every attempt of each, because trunk flaps on the same tests with every merge and a
single run turns each green flap into a false GENUINE. A job the window left UNMEASURED
(never a success, no identity: cancelled, cap-killed, still running) is read from the next
older verdict that measured it, and no deeper. A run re-running its failed jobs is in
progress again yet is still main's newest verdict, so it counts.

Results are cached for `CACHE_TTL_S` in the user cache directory.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from scripts.ci_failure_ids import failed_identities
from scripts.review_gh import TriageError, _gh

REPO = "private_owner/music-dj-tools"
MAIN_WINDOW = 3
VERDICT_DEPTH = 30
WORKFLOW_FILES = ("ci.yml", "e2e.yml")
CACHE_TTL_S = 600.0
LOG_READ_TRIES = 3
LOG_READ_BACKOFF_S = 10.0
# Walks that disagree with their own head are retried before the baseline gives up on
# being about a head at all: main merges in bursts, so one collision is ordinary.
SHA_PIN_TRIES = 3
CACHE_PATH = Path.home() / ".cache" / "opendj" / "ci-watch" / "main-red.json"


class LogUnreadable(Exception):
    """A job log that could not be read, so this job measured nothing."""

    def __init__(self, job_id: int) -> None:
        super().__init__(f"job {job_id}: log still empty after {LOG_READ_TRIES} reads")
        self.job_id = job_id


@dataclass(frozen=True)
class Verdict:
    run_id: int
    attempts: int
    # The COMMIT this run measured. The baseline is a claim about what it read, and without
    # this it had no way to say which commit that was.
    head_sha: str = ""


@dataclass(frozen=True)
class Job:
    job_id: int
    name: str
    conclusion: str | None


@dataclass(frozen=True)
class MainRed:
    identities: frozenset[str]
    failed_job_names: frozenset[str]
    main_sha: str
    unreadable_job_names: frozenset[str] = frozenset()
    # The newest commit the walk actually measured, which is NOT main's head whenever the
    # head's own run has not finished. `main_sha` says when this baseline was taken;
    # `measured_sha` says what it is about, and they are only the same commit sometimes.
    measured_sha: str = ""
    # The whole set, so staleness is an INVARIANT (every measurement came from the pinned
    # head) rather than a comparison against one representative value.
    measured_shas: frozenset[str] = frozenset()
    # Every identity, keyed by the commit its OWN measurement came from (not the walk's
    # newest). A pull request's merge base is compared against this, per identity, rather
    # than against one representative sha: main-at-C's red only excuses a pull request that
    # has not advanced past C, and a baseline spans several commits at once (issue #3344).
    identities_by_sha: dict[str, frozenset[str]] = field(default_factory=dict)


# ----- pure: the window walk -----


def verdict_runs(runs: Iterable[dict]) -> list[Verdict]:
    """Main's newest verdicts: a success or failure, or a rerun still in progress."""
    kept = [
        Verdict(run["id"], run["run_attempt"], run.get("head_sha", ""))
        for run in runs
        if run.get("conclusion") in ("success", "failure")
        or (run.get("status") != "completed" and run.get("run_attempt", 1) > 1)
    ]
    return kept[:VERDICT_DEPTH]


@dataclass(frozen=True)
class WalkResult:
    identities: frozenset[str]
    failed_job_names: frozenset[str]
    unreadable_job_names: frozenset[str] = frozenset()
    # The NEWEST commit any of this walk's measurements came from. Sol's P1 on #3293: the
    # result used to be stamped with main's head whatever it had actually read, and main's
    # head usually has no completed run, so the stamp named a commit the walk never saw.
    measured_sha: str = ""
    # EVERY commit that contributed a measurement, not just the newest. A walk spans several
    # workflow files, and they do not finish on the same commit: keeping one SHA let a
    # baseline whose `ci.yml` half was measured at the pinned head carry an `e2e.yml` half
    # measured at an older one, and be stamped current. Sol's P1 on #3293.
    measured_shas: frozenset[str] = frozenset()
    # Same key as `MainRed.identities_by_sha`, one workflow's share of it.
    identities_by_sha: dict[str, frozenset[str]] = field(default_factory=dict)


def _named_failures(
    job: Job, identities_of: Callable[[int], frozenset[str]]
) -> frozenset[str] | None:
    """What a FAILED job's log says failed, or None when the walk cannot use it.

    None covers two cases that are different facts about the log and the same fact about
    this walk, which is why they share a branch at the call site.

    The log could not be READ: UNMEASURED, and TERMINAL for this name. The name is not
    retained as a failure, because retained, a pull request job with the same name and no
    identity reads BASELINE_MISMATCH off a baseline nobody measured. Nor does an older run
    get to answer in its place: an older run's identities are exactly what
    newest-measured-wins refuses to subtract, and an older run's SUCCESS cannot refute a
    newer red whose cause could not be read.

    The log was READ and named nothing: the name alone then says main is red here and NOT
    what failed, so a pull request job with the same name and no identity would be compared
    off a comparison nobody made. A bundle budget check is the worked case, main 79 KB over
    and the pull request 300 KB over sharing one name.
    """
    try:
        ids = identities_of(job.job_id)
    except LogUnreadable:
        return None
    return ids or None


def _newest_measured(contributing: list[str]) -> str:
    """The newest commit that contributed a measurement, or empty when none did. Verdicts are
    walked newest first, so the first non-empty entry is the newest. Kept out of the walk
    itself, which sits on the complexity ceiling.
    """
    return next((sha for sha in contributing if sha), "")


def main_red_identities(
    verdicts: list[Verdict],
    jobs_of: Callable[[int, int], list[Job]],
    identities_of: Callable[[int], frozenset[str]],
    window: int = MAIN_WINDOW,
) -> WalkResult:
    """Walk main's verdicts. Failed job NAMES come from the same reads as identities: main's
    head alone misses a job red on every measured run while main's newest run is still
    queued (Wed 16 Sep 2026: the frontend bundle budget, issue #3276, red on main's last 4
    measured runs, read as GENUINE on a Python-only pull request).
    """
    failed_names: set[str] = set()
    unreadable: set[str] = set()
    found: set[str] = set()
    pending: set[str] = set()
    remaining_window = window
    # NEWEST-MEASURED-WINS. A job NAME is decided by the newest run that MEASURED it, and
    # every older run under that name is main's PAST. A plain union over the window was the
    # whole rule, and it retained identity A from an older run when the newest run of the
    # same job failed only on B: a pull request reintroducing A then read KNOWN_RED off a
    # failure main no longer has, and merged. Sol's P1 on #3293. The window still earns its
    # keep, filling in jobs the newest run never measured (queued, skipped, cancelled,
    # unreadable log); it just no longer speaks for a job a newer run already answered.
    decided: set[str] = set()
    identities_by_sha: dict[str, set[str]] = {}
    # EVERY verdict the RESULT depends on, newest first. Recording only the newest was Sol's
    # second P1 on #3293 and the same defect as the first, one level in: the window fills
    # names the newest verdict never measured from OLDER verdicts at other commits, so a
    # baseline whose newest verdict sits on the pinned head could carry an obsolete identity
    # from an older one and still be stamped fresh.
    #
    # Keyed on what a verdict DECIDED, not on what it measured. A verdict that only
    # re-measures a name an earlier one already decided adds nothing to the result, and
    # counting it would report staleness for a baseline that has none. That direction is not
    # free: every stale baseline degrades a verdict to UNKNOWN, so over-reporting staleness
    # would quietly retire the tool rather than fail loudly.
    contributing: list[str] = []
    for verdict in verdicts:
        decided_before = len(decided)
        measured: set[str] = set()
        # NEWEST attempt first, for the same reason verdicts are walked newest first: a
        # rerun that SUCCEEDS supersedes the attempt it reran. Oldest first, attempt 1's
        # identities survived attempt 2 going green, and a pull request reintroducing that
        # failure read KNOWN_RED.
        for attempt in range(verdict.attempts, 0, -1):
            jobs = list(jobs_of(verdict.run_id, attempt))
            # Resolved WITHIN the attempt before precedence applies across attempts, so the
            # order GitHub happens to return one attempt's jobs in cannot change the answer.
            succeeded = {job.name for job in jobs if job.conclusion == "success"}
            for job in jobs:
                if remaining_window > 0:
                    pending.add(job.name)
                elif job.name not in pending:
                    continue
                if job.name in decided:
                    # A newer attempt or verdict already measured this name. Counted
                    # measured so the window can close, never read for identities.
                    measured.add(job.name)
                    continue
                if job.conclusion == "success" or job.name in succeeded:
                    measured.add(job.name)
                    decided.add(job.name)
                elif job.conclusion == "failure":
                    ids = _named_failures(job, identities_of)
                    if ids is None:
                        unreadable.add(job.name)
                        decided.add(job.name)
                        continue
                    failed_names.add(job.name)
                    found |= ids
                    # This job's identities all came from THIS verdict's head, so that is
                    # the commit a pull request's merge base is compared against for them.
                    identities_by_sha.setdefault(verdict.head_sha, set()).update(ids)
                    measured.add(job.name)
                    decided.add(job.name)
        if len(decided) != decided_before:
            contributing.append(verdict.head_sha)
        pending -= measured
        remaining_window = max(remaining_window - 1, 0)
        if remaining_window == 0 and not pending:
            break
    return WalkResult(
        frozenset(found),
        frozenset(failed_names),
        frozenset(unreadable - failed_names),
        _newest_measured(contributing),
        frozenset(contributing) - {""},
        {sha: frozenset(ids) for sha, ids in identities_by_sha.items()},
    )


# ----- I/O: gh -----


def _json(path: str) -> dict:
    return json.loads(_gh(["api", path]))


def _jobs_of(run_id: int, attempt: int) -> list[Job]:
    payload = _json(f"repos/{REPO}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    return [Job(job["id"], job["name"], job.get("conclusion")) for job in payload["jobs"]]


def _fetch_job_log(job_id: int) -> str:
    return _gh(["api", "--allow-escape-sequences", f"repos/{REPO}/actions/jobs/{job_id}/logs"])


def job_log_identities(
    job_id: int,
    fetch: Callable[[int], str] = _fetch_job_log,
    sleep: Callable[[float], None] = time.sleep,
) -> frozenset[str]:
    """Identities from a job's FULL log (`gh run view --log-failed` truncates), read with
    retries: an EMPTY read is gh failing (rate wall, transient API error), not a job that
    failed without failing a test. Measured Wed 16 Sep 2026: without the retry the baseline
    read 39 identities where the reference implementation read 118, twelve minutes apart on
    the same main SHA, because empty reads dropped whole jobs silently.
    """
    return failed_identities(read_job_log(job_id, fetch, sleep))


def read_job_log(
    job_id: int,
    fetch: Callable[[int], str] = _fetch_job_log,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """A job's full log, retried while the read comes back empty.

    Still empty after every try means the log could not be READ, which is not the same as a
    job that failed no test, so it raises rather than returning a value a caller would
    classify. An empty string here would read as "this job names no failing test", and that
    is how an unmeasured job earns a verdict it did not deserve.
    """
    for attempt in range(LOG_READ_TRIES):
        try:
            log = fetch(job_id)
        except TriageError:
            # gh REFUSING the read is the same outcome as an empty one: nothing was
            # measured. GitHub expires job logs, so an older job in the baseline window
            # answers 404, and raising here killed the WHOLE baseline over one expired
            # log rather than leaving that one job unmeasured. Hit live Wed 16 Sep 2026.
            log = ""
        if log:
            return log
        if attempt + 1 < LOG_READ_TRIES:
            sleep(LOG_READ_BACKOFF_S)
    raise LogUnreadable(job_id)


def _main_sha() -> str:
    return _json(f"repos/{REPO}/commits/main")["sha"]


def _failed_check_run_names(sha: str) -> frozenset[str]:
    payload = _json(f"repos/{REPO}/commits/{sha}/check-runs?per_page=100")
    return frozenset(
        run["name"] for run in payload["check_runs"] if run.get("conclusion") == "failure"
    )


def unmeasured_baseline_names(
    unreadable: frozenset[str], failed_check_runs: frozenset[str], measured: frozenset[str]
) -> frozenset[str]:
    """Names main failed under, where nothing read a log to say WHAT failed.

    A failed check run at main's head carries a conclusion and nothing else: this tool never
    parsed a log for it, so it establishes that main is red under that name and not that a
    pull request failing under the same name failed the same way. Counted red, such a name
    is compared against a zero-identity pull request failure and the agent merges off
    a baseline nobody measured. The bundle budget check is the case that makes it concrete,
    where main can be 79 KB over and the pull request 300 KB over under one name.

    Counted unmeasured, the watch exits UNKNOWN and a person looks, which is the direction
    this tool is allowed to be wrong in.
    """
    return (unreadable | failed_check_runs) - measured


def stale_red_identities(
    identities_by_sha: dict[str, frozenset[str]],
    pr_head_sha: str,
    advanced_past: Callable[[str, str], bool],
) -> frozenset[str]:
    """Identities this pull request has already advanced past the measurement of (#3344).

    Main-at-C's red only excuses a pull request that has NOT advanced past C: a later main
    commit can fix C's failure, and a pull request based after that fix reintroducing it
    would otherwise read KNOWN_RED off a baseline measured before the fix existed. Bounded
    per SOURCE COMMIT rather than wholesale against main's live tip, which on current
    numbers is stale on ~87% of polls whether or not it matters to the pull request being
    watched. `identities_by_sha` empty (no per-commit provenance) yields nothing stale, the
    same as before this existed.

    `advanced_past` is a seam over `git merge-base --is-ancestor measured_sha pr_head_sha`;
    an undeterminable answer must come back True (advanced past), never False, or an ancestry
    check that fails closed would open the same excuse this function exists to close.
    """
    return frozenset(
        identity
        for sha, identities in identities_by_sha.items()
        if advanced_past(sha, pr_head_sha)
        for identity in identities
    )


def _runs_of(workflow: str) -> list[dict]:
    return _json(f"repos/{REPO}/actions/workflows/{workflow}/runs?branch=main&per_page=50")[
        "workflow_runs"
    ]


def _walk_every_workflow(
    jobs_of: Callable[[int, int], list[Job]] = _jobs_of,
    identities_of: Callable[[int], frozenset[str]] = job_log_identities,
    runs_of: Callable[[str], list[dict]] = _runs_of,
) -> WalkResult:
    identities: set[str] = set()
    failed_names: set[str] = set()
    unreadable: set[str] = set()
    measured: list[str] = []
    identities_by_sha: dict[str, frozenset[str]] = {}
    for workflow in WORKFLOW_FILES:
        walk = main_red_identities(verdict_runs(runs_of(workflow)), jobs_of, identities_of)
        identities |= walk.identities
        failed_names |= walk.failed_job_names
        unreadable |= walk.unreadable_job_names
        measured.extend(walk.measured_shas)
        for sha, ids in walk.identities_by_sha.items():
            identities_by_sha[sha] = identities_by_sha.get(sha, frozenset()) | ids
    contributing = frozenset(sha for sha in measured if sha)
    return WalkResult(
        frozenset(identities),
        frozenset(failed_names),
        frozenset(unreadable),
        # Still the newest single commit, for the human-readable line only. Nothing DECIDES
        # on it; the set below is what staleness is read from.
        max(contributing) if len(contributing) == 1 else next((s for s in measured if s), ""),
        contributing,
        identities_by_sha,
    )


def unattributable_baseline(sha: str, failed_check_runs: frozenset[str]) -> MainRed:
    """A baseline that subtracts NOTHING, for a walk no single head can be held to.

    Main moved under every attempt, so the identities the walk collected belong to a mixture
    of heads and there is no head they are all true of. Returned as a baseline anyway they
    are subtracted from pull requests for the cache's whole TTL, which is how a genuine
    regression reads as main's known red. Empty, every pull request failure reads GENUINE and
    a person looks: loud, and the direction this tool is allowed to be wrong in.
    """
    return MainRed(frozenset(), frozenset(), sha, failed_check_runs, "")


def fetch_main_red(
    main_sha: Callable[[], str] = _main_sha,
    walk: Callable[[], WalkResult] = _walk_every_workflow,
    failed_check_runs: Callable[[str], frozenset[str]] = _failed_check_run_names,
    tries: int = SHA_PIN_TRIES,
) -> MainRed:
    """Main's red, PINNED to one head: the walk is bracketed by two reads of main's SHA and
    the result is kept only when they agree.

    Sol's P1 on #3293. The head used to be read AFTER the walk, so a merge landing during a
    walk that takes minutes stamped failures measured at the OLD head with the NEW one. The
    cache then served them for its whole TTL as that head's baseline, and a pull request
    reintroducing a failure trunk repair had just fixed was subtracted as main's known red
    and merged. The stamp is what makes the result a claim about a head, so it has to be a
    head the whole walk saw.
    """
    for _ in range(tries):
        pinned = main_sha()
        result = walk()
        if main_sha() != pinned:
            continue
        if result.unreadable_job_names:
            print(
                f"[ci-main-red] baseline unmeasured for {sorted(result.unreadable_job_names)}",
                file=sys.stderr,
            )
        measured = frozenset(result.failed_job_names)
        if result.measured_shas and result.measured_shas != frozenset({pinned}):
            # Sol's P1 on #3293. Main's head usually has NO completed run -- 8 of its newest
            # 60 ci.yml runs are a completed verdict, 49 were cancelled by the tip-only
            # sweeper -- so the walk normally reads older commits. Said out loud rather than
            # stamped over, because a baseline that names the head it did not read is the
            # thing that let a failure trunk repair had just fixed read as main's red.
            print(
                "[ci-main-red] baseline measured at "
                + ", ".join(sorted(sha[:9] for sha in result.measured_shas))
                + f", main head is {pinned[:9]}",
                file=sys.stderr,
            )
        return MainRed(
            result.identities,
            measured,
            pinned,
            unmeasured_baseline_names(
                result.unreadable_job_names, failed_check_runs(pinned), measured
            ),
            result.measured_sha,
            result.measured_shas,
            result.identities_by_sha,
        )
    head = main_sha()
    print(
        f"[ci-main-red] main moved under {tries} walk(s); baseline subtracts nothing at {head}",
        file=sys.stderr,
    )
    return unattributable_baseline(head, failed_check_runs(head))


def cached_main_red(
    cache_path: Path = CACHE_PATH,
    now: Callable[[], float] = time.time,
    main_sha: Callable[[], str] = _main_sha,
    fetch: Callable[[], MainRed] = fetch_main_red,
) -> MainRed:
    """The cached baseline, reused only while it is BOTH fresh and about the current main.

    Age alone was the whole test, and main moves inside the TTL: trunk repair merges a fix
    and for up to `CACHE_TTL_S` afterwards a pull request reintroducing that same failure is
    still subtracted as main's red and exits mergeable. Re-reading main's head is one cheap
    call against the whole verdict walk this cache exists to avoid, so the check is cheap
    enough to make unconditionally. A head that cannot be read is not a match: the walk runs.
    """
    if cache_path.exists() and now() - cache_path.stat().st_mtime < CACHE_TTL_S:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        try:
            current = main_sha()
        except TriageError:
            current = ""
        # `measured_sha` in the payload is a VERSION gate: a cache written before the walk
        # recorded which commit it read cannot say what it is about, and a baseline that
        # cannot say that is re-walked rather than reused.
        if (
            "unreadable_job_names" in cached
            and "measured_shas" in cached
            and "identities_by_sha" in cached
            and current
            and cached["main_sha"] == current
        ):
            return MainRed(
                frozenset(cached["identities"]),
                frozenset(cached["failed_job_names"]),
                cached["main_sha"],
                frozenset(cached["unreadable_job_names"]),
                cached["measured_sha"],
                frozenset(cached["measured_shas"]),
                {sha: frozenset(ids) for sha, ids in cached["identities_by_sha"].items()},
            )
    fresh = fetch()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(f".tmp.{time.time_ns()}")
    tmp.write_text(
        json.dumps(
            {
                "identities": sorted(fresh.identities),
                "failed_job_names": sorted(fresh.failed_job_names),
                "main_sha": fresh.main_sha,
                "unreadable_job_names": sorted(fresh.unreadable_job_names),
                "measured_sha": fresh.measured_sha,
                "measured_shas": sorted(fresh.measured_shas),
                "identities_by_sha": {
                    sha: sorted(ids) for sha, ids in fresh.identities_by_sha.items()
                },
            }
        ),
        encoding="utf-8",
    )
    tmp.replace(cache_path)
    return fresh
