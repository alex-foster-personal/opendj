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
from dataclasses import dataclass
from pathlib import Path

from scripts.ci_failure_ids import failed_identities
from scripts.review_gh import TriageError, _gh

REPO = "maintainer/music-dj-tools"
MAIN_WINDOW = 3
VERDICT_DEPTH = 30
WORKFLOW_FILES = ("ci.yml", "e2e.yml")
CACHE_TTL_S = 600.0
LOG_READ_TRIES = 3
LOG_READ_BACKOFF_S = 10.0
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


# ----- pure: the window walk -----


def verdict_runs(runs: Iterable[dict]) -> list[Verdict]:
    """Main's newest verdicts: a success or failure, or a rerun still in progress."""
    kept = [
        Verdict(run["id"], run["run_attempt"])
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
    for verdict in verdicts:
        measured: set[str] = set()
        for attempt in range(1, verdict.attempts + 1):
            for job in jobs_of(verdict.run_id, attempt):
                if remaining_window > 0:
                    pending.add(job.name)
                elif job.name not in pending:
                    continue
                if job.conclusion == "success":
                    measured.add(job.name)
                elif job.conclusion == "failure":
                    try:
                        ids = identities_of(job.job_id)
                    except LogUnreadable:
                        # The name is NOT retained. Retained, a pull request job with the
                        # same name and no identity reads MAIN_RED_JOB and merges off a
                        # baseline that was never measured.
                        unreadable.add(job.name)
                        continue
                    failed_names.add(job.name)
                    if ids:
                        found |= ids
                        measured.add(job.name)
        pending -= measured
        remaining_window = max(remaining_window - 1, 0)
        if remaining_window == 0 and not pending:
            break
    return WalkResult(
        frozenset(found), frozenset(failed_names), frozenset(unreadable - failed_names)
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
    reads MAIN_RED_JOB against a zero-identity pull request failure and the agent merges off
    a baseline nobody measured. The bundle budget check is the case that makes it concrete,
    where main can be 79 KB over and the pull request 300 KB over under one name.

    Counted unmeasured, the watch exits UNKNOWN and a person looks, which is the direction
    this tool is allowed to be wrong in.
    """
    return (unreadable | failed_check_runs) - measured


def fetch_main_red() -> MainRed:
    identities: set[str] = set()
    failed_names: set[str] = set()
    unreadable: set[str] = set()
    for workflow in WORKFLOW_FILES:
        runs = _json(f"repos/{REPO}/actions/workflows/{workflow}/runs?branch=main&per_page=50")
        walk = main_red_identities(
            verdict_runs(runs["workflow_runs"]), _jobs_of, job_log_identities
        )
        identities |= walk.identities
        failed_names |= walk.failed_job_names
        unreadable |= walk.unreadable_job_names
    sha = _main_sha()
    if unreadable:
        print(
            f"[ci-main-red] baseline unmeasured for {sorted(unreadable)}", file=sys.stderr
        )
    measured = frozenset(failed_names)
    return MainRed(
        frozenset(identities),
        measured,
        sha,
        unmeasured_baseline_names(frozenset(unreadable), _failed_check_run_names(sha), measured),
    )


def cached_main_red(cache_path: Path = CACHE_PATH, now: Callable[[], float] = time.time) -> MainRed:
    if cache_path.exists() and now() - cache_path.stat().st_mtime < CACHE_TTL_S:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if "unreadable_job_names" in cached:
            return MainRed(
                frozenset(cached["identities"]),
                frozenset(cached["failed_job_names"]),
                cached["main_sha"],
                frozenset(cached["unreadable_job_names"]),
            )
    fresh = fetch_main_red()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(f".tmp.{time.time_ns()}")
    tmp.write_text(
        json.dumps(
            {
                "identities": sorted(fresh.identities),
                "failed_job_names": sorted(fresh.failed_job_names),
                "main_sha": fresh.main_sha,
                "unreadable_job_names": sorted(fresh.unreadable_job_names),
            }
        ),
        encoding="utf-8",
    )
    tmp.replace(cache_path)
    return fresh
