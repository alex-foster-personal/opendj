"""Iteration-speed metrics: the shared store, CI job recording, and check 5.

One store, two writers. scripts/iteration_metrics.sh appends local `just` timings; this
module appends per-job CI durations pulled from the gh API. Both use the same schema
{ts, step, seconds, sha, host, exit}, so a slow gate on the maintainer's Mac and a slow gate on a
GitHub runner sit on one axis and are compared by one median.

Check 5 flags any step whose NEWEST timing exceeds ITERATION_SPEED_REGRESSION_FACTOR times
the median of its preceding ITERATION_SPEED_MEDIAN_WINDOW records, exiting
EXIT_ITERATION_SPEED so it routes through the watchdog's existing alert path unchanged.

WHY THE NEWEST RECORD IS EXCLUDED FROM ITS OWN MEDIAN
    A median that includes the sample under test moves toward it and blunts the very
    signal being looked for. The comparison is always newest-versus-history.

WHY 'insufficient-data' IS ok=True HERE, UNLIKE CHECKS 1 AND 3
    Those two go ok=False on a thin sample because insufficient data means they cannot
    tell whether CI is dead, and a silent pass would hide an outage. A thin iteration
    history means only that no regression is computable yet - CI itself is still fully
    covered by checks 1-4. Alerting on it would page the maintainer every 4 hours from a fresh
    machine until history accrued, which trains him to ignore the watchdog. It is printed
    in the verdict line on every single run, so it is visible, not silent.

FAIL-OPEN BOUNDARY
    scripts/iteration_metrics.sh is fail-open on its metrics WRITE by design: a broken
    store must never break a build. This module is NOT. It feeds a watchdog, so an
    unreadable or malformed store is exactly the kind of quiet rot the watchdog exists to
    surface, and it raises PreconditionError rather than carrying on with partial data.

MINI-PRD
    R1 Iteration-speed regression ................................ done + ran + regression
       Acceptance tests:
         [if] a step's 20-run median is 20s and the newest run took 40s
              [then] verdict ERROR, classification 'iteration-speed', exit 6 (*)
         [if] a step's 20-run median is 20s and the newest run took 25s
              [then] verdict OK - 1.25x is under the 1.5x factor (*)
         [if] a step has fewer than ITERATION_SPEED_MIN_SAMPLE prior records
              [then] verdict OK, classification 'insufficient-data', naming the shortfall (*)
         [if] a step's median is under ITERATION_SPEED_MIN_SECONDS
              [then] verdict OK - 1.5x of a sub-threshold timing is scheduler noise (*)
    R2 Honest store parsing ...................................... done + ran + regression
       Acceptance tests:
         [if] the store holds a malformed line
              [then] raise PreconditionError, never skip the line silently (*)
         [if] the store is absent
              [then] return no records - a machine that never timed anything is not a fault (*)
    R3 Trustworthy CI durations .................................. done + ran + regression
       Acceptance tests:
         [if] a 'skipped' job reports completed_at before started_at
              [then] it is excluded, never recorded as a negative duration (*)
         [if] a job that DID execute reports a negative duration
              [then] raise PreconditionError, never drop it quietly (*)
         [if] the same job is seen twice by the 4-hourly poll
              [then] it is recorded once - a median must describe the build, not the cadence (*)

-Claude
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scripts.ci_health_core import (
    EXIT_ITERATION_SPEED,
    EXIT_OK,
    REPO,
    CheckResult,
    PreconditionError,
    Run,
    _gh_api_json,
    _parse_github_timestamp,
)

# ----- configuration ---------------------------------------------------------------

# The store is shared with scripts/iteration_metrics.sh and is machine-local, never
# committed.
ITERATION_METRICS_PATH = (
    Path.home() / ".local" / "share" / "mdt-iteration-metrics" / "metrics.jsonl"
)

ITERATION_SPEED_MEDIAN_WINDOW = 20
ITERATION_SPEED_REGRESSION_FACTOR = 1.5
# How many of the newest records are medianed together and compared to the baseline. One
# record is outlier detection and pages on noise; three is change detection, which is the
# actual question. Calibrated against a real false positive on the first live run, see
# _check_iteration_speed.
ITERATION_SPEED_RECENT_WINDOW = 3
# Below this, a step needs more history than a fresh store can have. Five prior records is
# the point where one unlucky run stops being able to move the median on its own.
ITERATION_SPEED_MIN_SAMPLE = 5
# 1.5x of a 2s step is 3s, which is runner scheduling jitter, not a regression. Only steps
# whose median is genuinely slow enough to cost iteration time are judged.
ITERATION_SPEED_MIN_SECONDS = 10.0
# How many of the newest completed runs to pull job durations for. Each one costs a gh API
# call, and the 4-hourly cadence means 10 covers the window several times over.
JOB_FETCH_RUN_COUNT = 10
CI_METRIC_SOURCE = "ci"
CI_METRIC_HOST = "github-actions"
# Jobs that never executed. GitHub still stamps them completed, and for a skipped job it
# stamps completed_at BEFORE started_at, which yields a negative duration: observed live
# Thu 20 Aug 2026 on this repo as -1s for 'quality ratchet' and -11s for 'Windows Rekordbox
# parity gate', both conclusion 'skipped'. A cancelled job has a real but truncated
# duration, which is equally meaningless as an iteration-speed measurement. None of them
# describe how long the work takes, so none of them belong in a median.
NON_EXECUTING_JOB_CONCLUSIONS = ("skipped", "cancelled", "neutral")

# ----- data ------------------------------------------------------------------------


@dataclass
class Job:
    """One completed CI job, the unit iteration speed is actually measured in."""

    job_id: int
    workflow: str
    name: str
    head_sha: str
    conclusion: str
    started_at: datetime
    completed_at: datetime

    @property
    def duration_seconds(self) -> float:
        return (self.completed_at - self.started_at).total_seconds()

    @property
    def step(self) -> str:
        """Store key. Namespaced so a CI job never collides with a local `just` step."""
        return f"ci:{self.workflow}/{self.name}"


@dataclass
class Metric:
    """One line of the iteration-metrics store, from either writer."""

    ts: str
    step: str
    seconds: float
    source: str
    job_id: int | None


# ----- CI job durations --------------------------------------------------------------


def _fetch_jobs(runs: list[Run]) -> list[Job]:
    """Completed jobs for the newest JOB_FETCH_RUN_COUNT runs, newest run first.

    Jobs still queued or in progress have a null completed_at and are skipped: they have no
    duration yet, and inventing one would put a fictional number into the median. Jobs that
    never executed are dropped for the same reason, see NON_EXECUTING_JOB_CONCLUSIONS.

    A job that DID execute and still reports a negative duration is unexplained, so it
    raises rather than being quietly dropped: silently discarding it would hide a real
    change in how GitHub reports timings behind a thinning sample nobody noticed.
    """
    jobs: list[Job] = []
    for run in runs[:JOB_FETCH_RUN_COUNT]:
        payload = _gh_api_json(f"repos/{REPO}/actions/runs/{run.run_id}/jobs?per_page=100")
        if not isinstance(payload, dict) or "jobs" not in payload:
            raise PreconditionError(f"runs/{run.run_id}/jobs response has no jobs key")
        for item in payload["jobs"]:
            if item.get("status") != "completed" or not item.get("completed_at"):
                continue
            if str(item.get("conclusion")) in NON_EXECUTING_JOB_CONCLUSIONS:
                continue
            job_id = item["id"]
            job = Job(
                job_id=int(job_id),
                workflow=run.name,
                name=str(item["name"]),
                head_sha=str(item["head_sha"])[:7],
                conclusion=str(item["conclusion"]),
                started_at=_parse_github_timestamp(item["started_at"], "started_at", job_id),
                completed_at=_parse_github_timestamp(item["completed_at"], "completed_at", job_id),
            )
            if job.duration_seconds < 0:
                raise PreconditionError(
                    f"job {job_id} ({job.name}, conclusion {job.conclusion}) reports a "
                    f"negative duration of {job.duration_seconds:.0f}s - it completed at "
                    f"{item['completed_at']} but started at {item['started_at']}"
                )
            jobs.append(job)
    return jobs


# ----- iteration metrics store -----------------------------------------------------


def _read_metrics(path: Path) -> list[Metric]:
    """Parse the shared store, newest last. A malformed line is fatal, never skipped.

    An absent store is NOT an error: it is simply a machine where nothing has been timed
    yet, and check 5 reports that shortfall rather than manufacturing a verdict from it.
    """
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PreconditionError(f"iteration metrics store {path} is unreadable: {exc}") from exc

    metrics: list[Metric] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PreconditionError(f"{path}:{number} is not valid JSON: {exc}") from exc
        missing = [key for key in ("ts", "step", "seconds") if key not in record]
        if missing:
            raise PreconditionError(f"{path}:{number} is missing required keys: {missing}")
        try:
            seconds = float(record["seconds"])
        except (TypeError, ValueError) as exc:
            raise PreconditionError(
                f"{path}:{number} has a non-numeric seconds value: {record['seconds']!r}"
            ) from exc
        job_id = record.get("job_id")
        metrics.append(
            Metric(
                ts=str(record["ts"]),
                step=str(record["step"]),
                seconds=seconds,
                source=str(record.get("source", "local")),
                job_id=int(job_id) if job_id is not None else None,
            )
        )
    return metrics


def _record_job_metrics(jobs: list[Job], existing: list[Metric], path: Path) -> int:
    """Append durations for jobs not already in the store. Returns how many were written.

    Deduplicated by job_id because the watchdog runs every 4 hours over an overlapping
    window: without this, the same job would be counted six times a day and the median
    would describe the polling cadence rather than the build.
    """
    known = {metric.job_id for metric in existing if metric.job_id is not None}
    fresh = [job for job in jobs if job.job_id not in known]
    if not fresh:
        return 0

    # Oldest first, so the store stays in chronological append order like the shim's writes.
    fresh.sort(key=lambda job: job.completed_at)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for job in fresh:
                handle.write(
                    json.dumps(
                        {
                            "ts": job.completed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "step": job.step,
                            "seconds": round(job.duration_seconds, 3),
                            "sha": job.head_sha,
                            "host": CI_METRIC_HOST,
                            "exit": 0 if job.conclusion == "success" else 1,
                            "source": CI_METRIC_SOURCE,
                            "job_id": job.job_id,
                        }
                    )
                    + "\n"
                )
    except OSError as exc:
        raise PreconditionError(f"cannot append CI durations to {path}: {exc}") from exc
    return len(fresh)


# ----- check 5 -----------------------------------------------------------------------


def _check_iteration_speed(metrics: list[Metric]) -> CheckResult:
    """(e) A step whose RECENT timings sit above its own rolling median.

    Per step: median the newest ITERATION_SPEED_RECENT_WINDOW records and compare that to
    the median of the ITERATION_SPEED_MEDIAN_WINDOW records preceding them. The recent
    records are deliberately NOT in the baseline median, because a median that includes the
    samples under test moves toward them and blunts the very signal being looked for.

    WHY A RECENT MEDIAN AND NOT THE SINGLE NEWEST RUN
        The first live run of this check compared one record and fired on
        'CI Cost Guard/Price completed workflow run' at 39s against a 14s median - 2.79x,
        and pure noise. That job's wall time is dominated by queueing and image pull, not
        by any work that changed. Comparing a 3-run median means one unlucky run can no
        longer page anyone, while a real regression, which by definition persists across
        runs, still does. This is change detection, not outlier detection: the question is
        whether the step is NOW slower, not whether one run was slow once.
    """
    by_step: dict[str, list[Metric]] = {}
    for metric in metrics:
        by_step.setdefault(metric.step, []).append(metric)

    factor = ITERATION_SPEED_REGRESSION_FACTOR
    recent_window = ITERATION_SPEED_RECENT_WINDOW
    worst: tuple[float, str, float, float, int] | None = None
    judged = 0

    for step, records in sorted(by_step.items()):
        if len(records) < recent_window + ITERATION_SPEED_MIN_SAMPLE:
            continue
        history = records[:-recent_window][-ITERATION_SPEED_MEDIAN_WINDOW:]
        baseline = statistics.median(record.seconds for record in history)
        if baseline < ITERATION_SPEED_MIN_SECONDS:
            continue
        judged += 1
        recent = statistics.median(record.seconds for record in records[-recent_window:])
        ratio = recent / baseline
        if ratio > factor and (worst is None or ratio > worst[0]):
            worst = (ratio, step, recent, baseline, len(history))

    if worst is not None:
        ratio, step, recent, baseline, size = worst
        return CheckResult(
            check="iteration-speed",
            ok=False,
            classification="iteration-speed",
            detail=(
                f"'{step}' is running at {recent:.1f}s across its newest {recent_window} "
                f"records against a {baseline:.1f}s median over the {size} before them "
                f"= {ratio:.2f}x, above the {factor:.1f}x limit"
            ),
            sample_size=size,
            exit_code=EXIT_ITERATION_SPEED,
            remediation=(
                f"Compare the newest '{step}' timings to the previous ones in "
                f"{ITERATION_METRICS_PATH}. A CI job (step prefixed 'ci:') points at the "
                "workflow; a local step points at this machine or the tree it measured."
            ),
        )

    if judged == 0:
        return CheckResult(
            check="iteration-speed",
            ok=True,
            classification="insufficient-data",
            detail=(
                f"no step yet has {recent_window} recent plus "
                f"{ITERATION_SPEED_MIN_SAMPLE} prior records and a median over "
                f"{ITERATION_SPEED_MIN_SECONDS:.0f}s, so no regression is computable "
                f"({len(by_step)} steps, {len(metrics)} records in the store)"
            ),
            sample_size=len(metrics),
            exit_code=EXIT_OK,
        )

    return CheckResult(
        check="iteration-speed",
        ok=True,
        classification="healthy",
        detail=(
            f"{judged} of {len(by_step)} steps had enough history to judge, "
            f"none over {factor:.1f}x its own rolling median"
        ),
        sample_size=judged,
        exit_code=EXIT_OK,
    )
