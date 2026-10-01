#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""CI health watchdog - detects the CI failure classes that are INVISIBLE from inside CI.

Runs OUTSIDE GitHub Actions on purpose. The two failure classes this exists to catch both
present as silence inside CI itself, so a workflow cannot be the thing that reports them:

  1. BILLING REFUSAL - a spending-limit / payment refusal kills every run before any step
     executes. An in-CI monitor never starts in that condition and therefore
     cannot alert; an external prober can observe the refusal signature.
  2. TRIGGER DRIFT - a malformed or over-narrowed `on:` block means runs never start at
     all. There is no failing run to notice; the repo just goes quiet while looking green.
     Triggers are fail-open, so absence of runs is the only signal.

Checks (c) and (d) are the ordinary-health backstop around those two, and check (e) is the
iteration-speed regression detector added Thu 20 Aug 2026.

Zero LLM tokens per check: pure stdlib plus `gh` subprocess calls.

ITERATION METRICS STORE
    Every run also RECORDS per-job CI durations into the shared machine-local store at
    ~/.local/share/mdt-iteration-metrics/metrics.jsonl, the same file scripts/
    iteration_metrics.sh appends local `just` timings to. One writer schema, one axis: a
    slow gate on this Mac and a slow gate on a GitHub runner are the same measurement
    taken in two places. Records are deduplicated by job_id, so the 4-hourly schedule
    re-reads the same runs without double-counting them.

    Recording is WATERMARK-based: the store's own newest `source: ci` record is the cursor,
    and one oldest-first bounded batch of runs whose jobs completed at or after it is
    fetched after listing at most JOB_FETCH_MAX_PAGES pages per poll. It used to take a
    fixed newest-10 slice of the runs
    checks 1-4 already had, on the assumption that 10 covered a 4-hour window several times
     over. Bursts can scroll past such a fixed slice and thin the sample. A
     catch-up deep enough to hit the page bound is named in text and --json
     output rather than dropped; see _fetch_completed_runs_since for why it
     is loud and not fatal.

USAGE
    uv run --no-project python -m scripts.ci_health_check
    uv run --no-project python -m scripts.ci_health_check --json
    just ci-health

Run as a MODULE from the repo root: the checks import scripts.ci_health_core and
scripts.ci_health_metrics, so the repo must be on sys.path. `--no-project` still applies -
this is stdlib-only and must keep working when the project venv does not.

SCHEDULING (machine-local, NOT committed)
    Plist:   ~/Library/LaunchAgents/com.YOU.mdt-ci-health.plist
    Runner:  scripts/ci_health_notify.sh  (invoked by the plist; alerts on nonzero exit)
    Log:     ~/Library/Logs/mdt-ci-health.log
    Install: see the `ci-health` recipe comment in the repo justfile.

EXIT CODES (lowest-numbered failing check wins, most severe first)
    0   every check passed
    2   billing / spending-limit refusal signature
    3   trigger drift (pushes landing, no runs starting)
    4   real-failure rate over threshold
    5   staleness (pushes landing, no completed runs at all)
    6   iteration-speed regression (a step got materially slower than its own median)
    10  precondition failure (gh missing, not authenticated, API error, unparseable data)

MINI-PRD
    R1 Billing-signature detection ............................... done + ran + regression
       Among the newest BILLING_SAMPLE_SIZE completed runs, classify a billing refusal when
       every run failed AND the median duration is under BILLING_MAX_MEDIAN_SECONDS.
       Acceptance tests:
         [if] the 8 newest completed runs are all 'failure' with a 4s median
              [then] verdict ERROR, classification 'billing', exit 2 ⛔️
         [if] the 8 newest completed runs are all 'failure' but the median is 200s
              [then] verdict OK for this check - that is real breakage, not a refusal,
              and check (c) is what should fire ⛔️
         [if] fewer than BILLING_MIN_SAMPLE completed runs exist at all
              [then] verdict ERROR 'insufficient sample', never a silent pass ⛔️
    R2 Trigger-drift detection ................................... done + ran + regression
       Commits pushed to the default branch inside TRIGGER_DRIFT_WINDOW_HOURS with zero
       workflow runs on that same branch in the same window means triggers are fail-open.
       Acceptance tests:
         [if] 6 commits landed on main in 48h and 0 runs carry head_branch main
              [then] verdict ERROR, classification 'trigger-drift', exit 3 ⛔️
         [if] 6 commits landed and 6 runs carry head_branch main
              [then] verdict OK ⛔️
         [if] 0 commits landed in the window
              [then] verdict OK - quiet repo is not drift, nothing was expected to fire ⛔️
    R3 Real-failure-rate detection ............................... done + ran + regression
       Classify the newest FAILURE_RATE_SAMPLE_SIZE completed runs from their JOBS, not
       the run top line, then count only failures longer than REAL_FAILURE_MIN_SECONDS.
       Unknown runs stay named and instant infrastructure refusals remain attributed to R1.
       Acceptance tests:
         [if] a cancelled run longer than 60s contains a failed job
              [then] it counts as a real failure and is named as buried ⛔️
         [if] eight real billing refusals contain failed jobs but last under 15s
              [then] verdict OK here (0 real failures) and R1 carries the alarm ⛔️
         [if] jobs never concluded or no jobs exist
              [then] they are named unknown, never described as passes ⛔️
    R4 Staleness detection ....................................... done + ran + regression
       No completed run at all within STALENESS_MAX_DAYS despite commits landing on the
       default branch in that window.
       Acceptance tests:
         [if] commits landed 3 days ago and the newest completed run is 9 days old
              [then] verdict ERROR, exit 5 ⛔️
         [if] the newest completed run is 2 hours old
              [then] verdict OK ⛔️
         [if] no commits landed in STALENESS_MAX_DAYS and no runs happened
              [then] verdict OK - dormant repo, nothing was expected ⛔️
    R5 Machine-readable output ................................... done + ran + regression
       --json emits the full result set including every denominator used.
       Acceptance tests:
         [if] --json is passed
              [then] stdout parses as JSON with keys repo, checked_at, exit_code, checks ⛔️
         [if] --json is passed
              [then] each check object carries its own sample_size denominator ⛔️
    R6 Self-remediation of CI .................................... out of scope
       This watchdog DETECTS and CLASSIFIES, then hands a human-actionable remediation
       line to the notifier. It deliberately does not mutate billing settings or push
       workflow fixes unattended.
    R7 Iteration-speed regression ................................ done + ran + regression
       Record per-job CI durations into the shared metrics store - watermark-cursored on
       the store's newest `source: ci` record, so a burst is caught up rather than clipped
       to a fixed newest-N slice - then flag any step whose NEWEST timing exceeds
       ITERATION_SPEED_REGRESSION_FACTOR times the median of its preceding
       ITERATION_SPEED_MEDIAN_WINDOW records. The newest record is excluded from its own
       median, otherwise a slowdown drags the very baseline it is measured against.
       Acceptance tests:
         [if] a step's 20-run median is 20s and the newest run took 40s
              [then] verdict ERROR, classification 'iteration-speed', exit 6 ⛔️
         [if] a step's 20-run median is 20s and the newest run took 25s
              [then] verdict OK - 1.25x is under the 1.5x factor ⛔️
         [if] a step has fewer than ITERATION_SPEED_MIN_SAMPLE prior records
              [then] verdict OK, classification 'insufficient-data', naming the shortfall ⛔️
         [if] a step's median is under ITERATION_SPEED_MIN_SECONDS
              [then] verdict OK - 1.5x of a sub-threshold timing is scheduler noise ⛔️
         [if] the metrics store holds a malformed line
              [then] raise PreconditionError, never skip the line silently ⛔️
         [if] a 'skipped' job reports completed_at before started_at
              [then] it is excluded, never recorded as a negative duration ⛔️
         [if] a job that DID execute reports a negative duration
              [then] raise PreconditionError, never drop it quietly ⛔️
         [if] a burst completes far more runs than the per-poll batch cap
              [then] the oldest batch is recorded, the newer remainder is named and resumes
              from the metric watermark on the next poll; a backlog deeper than
              JOB_FETCH_MAX_PAGES pages is also named on stdout and in --json
              (job_fetch_note) rather than silently dropped ⛔️
       WHY 'insufficient-data' IS OK=TRUE HERE, UNLIKE R1 AND R3
         Those two go ok=False on a thin sample because insufficient data means they cannot
         tell whether CI is dead, and a silent pass would hide an outage. A thin iteration
         history means only that no regression is computable yet - CI itself is still fully
         covered by checks 1-4. Alerting on it would page the maintainer every 4 hours from a fresh
         machine until history accrued, which trains him to ignore the watchdog. It is
         reported in the verdict line on every single run, so it is visible, not silent.

FAIL-OPEN BOUNDARY
    scripts/iteration_metrics.sh is fail-open on its metrics WRITE by design: a broken
    store must never break a build. This checker is NOT. It is a watchdog, so an
    unreadable or malformed store is exactly the kind of quiet rot it exists to surface,
    and it raises PreconditionError (exit 10) rather than carrying on with partial data.

HONEST DENOMINATORS
    Every rate in the output names the sample it was computed over. `sample_size` is the
    count of runs actually examined after EXCLUDED_RUN_EVENTS filtering, never the count
    requested. A rate quoted against an assumed denominator is worse than no rate.

-Claude
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

try:
    from scripts.ci_health_core import (
        EXIT_BILLING,
        EXIT_FAILURE_RATE,
        EXIT_OK,
        EXIT_PRECONDITION,
        EXIT_STALENESS,
        EXIT_TRIGGER_DRIFT,
        REPO,
        RUN_FETCH_COUNT,
        STALENESS_REMEDIATION,
        CheckResult,
        PreconditionError,
        Run,
        RunJobsPayloadCache,
        _cached_run_jobs_payload,
        _gh_api_json,
        _parse_completed_runs,
    )
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.ci_health_check") from None
    raise
from scripts.ci_health_metrics import (
    ITERATION_METRICS_PATH,
    _check_iteration_speed,
    _fetch_jobs,
    _read_metrics,
    _record_job_metrics,
)
from scripts.ci_health_trunk import _check_trunk_verified
from scripts.trunk_job_verdict_core import (
    VERDICT_FAILURE,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
    RunOutcome,
    RunVerdict,
    classify_run,
    parse_run_jobs,
)

# ----- configuration ---------------------------------------------------------------

BILLING_SAMPLE_SIZE = 8
BILLING_MIN_SAMPLE = 4
BILLING_MAX_MEDIAN_SECONDS = 15.0

TRIGGER_DRIFT_WINDOW_HOURS = 48

FAILURE_RATE_SAMPLE_SIZE = 20
FAILURE_RATE_MAX = 0.50
REAL_FAILURE_MIN_SECONDS = 60.0

STALENESS_MAX_DAYS = 7

# EXCLUDED_RUN_EVENTS and RUN_FETCH_COUNT moved to scripts.ci_health_core when the metrics
# catch-up became a second reader of the actions/runs listing. Same values, one home.

BILLING_REMEDIATION = (
    "Open GitHub Billing and plans -> https://github.com/settings/billing "
    "and clear the spending limit or payment failure, then re-run a workflow."
)

# ----- data ------------------------------------------------------------------------


def _fetch_default_branch() -> str:
    payload = _gh_api_json(f"repos/{REPO}")
    if not isinstance(payload, dict) or "default_branch" not in payload:
        raise PreconditionError(f"repos/{REPO} response has no default_branch")
    return str(payload["default_branch"])


def _fetch_completed_runs() -> list[Run]:
    """Newest-first completed runs, with synthesized non-Actions events filtered out.

    Page 1 only. Checks 1-4 all reason about the NEWEST runs, so a single page is the right
    sample; the metrics catch-up does its own deeper, watermark-bounded paging because it
    reasons about every run it has not yet recorded.
    """
    payload = _gh_api_json(f"repos/{REPO}/actions/runs?status=completed&per_page={RUN_FETCH_COUNT}")
    runs = _parse_completed_runs(payload)
    runs.sort(key=lambda run: run.started_at, reverse=True)
    return runs


def _fetch_default_branch_commit_count(default_branch: str, since: datetime) -> int:
    payload = _gh_api_json(
        "repos/{repo}/commits?sha={branch}&since={since}&per_page=100".format(
            repo=REPO, branch=default_branch, since=since.strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    )
    if not isinstance(payload, list):
        raise PreconditionError("commits response was not a list")
    return len(payload)


# ----- checks ----------------------------------------------------------------------


def _check_billing(runs: list[Run]) -> CheckResult:
    """(a) Every recent run failed almost instantly means the runner refused to start."""
    sample = runs[:BILLING_SAMPLE_SIZE]
    size = len(sample)
    if size < BILLING_MIN_SAMPLE:
        return CheckResult(
            check="billing",
            ok=False,
            classification="insufficient-data",
            detail=(
                f"only {size} completed runs available, "
                f"need {BILLING_MIN_SAMPLE} to classify the billing signature"
            ),
            sample_size=size,
            exit_code=EXIT_BILLING,
            remediation=STALENESS_REMEDIATION,
        )

    median_duration = statistics.median(run.duration_seconds for run in sample)
    failed = sum(1 for run in sample if run.is_failure)
    limit = BILLING_MAX_MEDIAN_SECONDS

    if failed == size and median_duration < limit:
        return CheckResult(
            check="billing",
            ok=False,
            classification="billing",
            detail=(
                f"billing/spending-limit refusal signature - all {size} newest "
                f"completed runs failed with a {median_duration:.1f}s median "
                f"(threshold {limit:.0f}s)"
            ),
            sample_size=size,
            exit_code=EXIT_BILLING,
            remediation=BILLING_REMEDIATION,
        )

    return CheckResult(
        check="billing",
        ok=True,
        classification="healthy",
        detail=(
            f"no refusal signature over the {size} newest completed runs "
            f"({failed} failed, {median_duration:.1f}s median)"
        ),
        sample_size=size,
        exit_code=EXIT_OK,
    )


def _check_trigger_drift(runs: list[Run], default_branch: str, now: datetime) -> CheckResult:
    """(b) Commits landing on the default branch with no runs starting means fail-open triggers."""
    hours = TRIGGER_DRIFT_WINDOW_HOURS
    window_start = now - timedelta(hours=hours)
    commits = _fetch_default_branch_commit_count(default_branch, window_start)
    branch_runs = [
        run for run in runs if run.head_branch == default_branch and run.started_at >= window_start
    ]

    if commits > 0 and not branch_runs:
        return CheckResult(
            check="trigger-drift",
            ok=False,
            classification="trigger-drift",
            detail=(
                f"{commits} commits landed on {default_branch} in the last {hours}h "
                "but zero workflow runs started for that branch - triggers are fail-open"
            ),
            sample_size=commits,
            exit_code=EXIT_TRIGGER_DRIFT,
            remediation=(
                "Inspect the `on:` blocks in .github/workflows/*.yml - a push/branches "
                f"filter that no longer matches {default_branch} is the usual cause."
            ),
        )

    return CheckResult(
        check="trigger-drift",
        ok=True,
        classification="healthy",
        detail=(
            f"{commits} commits and {len(branch_runs)} runs "
            f"on {default_branch} in the last {hours}h"
        ),
        sample_size=commits,
        exit_code=EXIT_OK,
    )


def _classify_failure_rate_runs(
    runs: list[Run], job_payloads: RunJobsPayloadCache
) -> list[tuple[Run, RunVerdict]]:
    """Classify each run from its jobs, reusing any payload metrics already fetched."""
    classified: list[tuple[Run, RunVerdict]] = []
    for run in runs:
        payload = _cached_run_jobs_payload(run.run_id, job_payloads, _gh_api_json)
        outcome = RunOutcome(
            run_id=run.run_id,
            head_sha=run.head_sha,
            run_conclusion=run.conclusion,
            jobs=parse_run_jobs(payload, run.run_id),
        )
        classified.append((run, classify_run(outcome)))
    return classified


def _check_failure_rate(
    runs: list[Run],
    job_payloads: RunJobsPayloadCache,
    sample_size: int = FAILURE_RATE_SAMPLE_SIZE,
) -> CheckResult:
    """(c) Genuine red CI, decided by jobs rather than a misleading run top line."""
    sample = runs[:sample_size]
    if not sample:
        return CheckResult(
            check="failure-rate",
            ok=False,
            classification="insufficient-data",
            detail="no completed runs available to compute a failure rate",
            sample_size=0,
            exit_code=EXIT_FAILURE_RATE,
            remediation=STALENESS_REMEDIATION,
        )

    size = len(sample)
    floor = REAL_FAILURE_MIN_SECONDS
    classified = _classify_failure_rate_runs(sample, job_payloads)
    failures = [
        (run, verdict)
        for run, verdict in classified
        if verdict.verdict == VERDICT_FAILURE
    ]
    real_failures = [item for item in failures if item[0].duration_seconds > floor]
    short_failures = len(failures) - len(real_failures)
    buried = sum(1 for _, verdict in real_failures if verdict.is_buried)
    passed = sum(1 for _, verdict in classified if verdict.verdict == VERDICT_PASS)
    unknown = sum(1 for _, verdict in classified if verdict.verdict == VERDICT_UNKNOWN)
    real = len(real_failures)
    rate = real / size
    headline = (
        f"{real}/{size} recent runs are real job failures "
        f"({buried} buried by run top line; {passed} pass, {unknown} unknown, "
        f"{short_failures} short job failures excluded; over {floor:.0f}s) "
        f"= {rate * 100:.0f} percent"
    )

    if rate > FAILURE_RATE_MAX:
        return CheckResult(
            check="failure-rate",
            ok=False,
            classification="failure-rate",
            detail=f"{headline}, above the {FAILURE_RATE_MAX * 100:.0f} percent limit",
            sample_size=size,
            exit_code=EXIT_FAILURE_RATE,
            remediation="Open the newest failing run: gh run list --limit 5",
        )

    return CheckResult(
        check="failure-rate",
        ok=True,
        classification="healthy",
        detail=headline,
        sample_size=size,
        exit_code=EXIT_OK,
    )


def _check_staleness(runs: list[Run], default_branch: str, now: datetime) -> CheckResult:
    """(d) Pushes landing but CI has been silent for a week."""
    days = STALENESS_MAX_DAYS
    window_start = now - timedelta(days=days)
    recent_runs = [run for run in runs if run.started_at >= window_start]
    if recent_runs:
        age_hours = (now - recent_runs[0].started_at).total_seconds() / 3600
        return CheckResult(
            check="staleness",
            ok=True,
            classification="healthy",
            detail=(
                f"{len(recent_runs)} completed runs in the last {days}d, "
                f"newest {age_hours:.1f}h old"
            ),
            sample_size=len(recent_runs),
            exit_code=EXIT_OK,
        )

    commits = _fetch_default_branch_commit_count(default_branch, window_start)
    if commits > 0:
        return CheckResult(
            check="staleness",
            ok=False,
            classification="staleness",
            detail=(f"no completed run in {days}d despite {commits} commits on {default_branch}"),
            sample_size=commits,
            exit_code=EXIT_STALENESS,
            remediation=STALENESS_REMEDIATION,
        )

    return CheckResult(
        check="staleness",
        ok=True,
        classification="healthy",
        detail=(f"no runs and no commits in {STALENESS_MAX_DAYS}d - dormant, nothing was expected"),
        sample_size=0,
        exit_code=EXIT_OK,
    )


# ----- orchestration ---------------------------------------------------------------


def _collect_results() -> tuple[list[CheckResult], str, int, str | None]:
    now = datetime.now(UTC)
    default_branch = _fetch_default_branch()
    runs = _fetch_completed_runs()

    # Record before judging, so this run's own CI durations are in the store that check 5
    # reads. The newest job is then measured against the history that preceded it. The
    # store is also the cursor: _fetch_jobs reads its newest CI record as the watermark and
    # catches up from there, so `existing` has to be read before the fetch, not after.
    existing = _read_metrics(ITERATION_METRICS_PATH)
    job_payloads: RunJobsPayloadCache = {}
    outcome = _fetch_jobs(runs, existing, job_payloads)
    recorded = _record_job_metrics(outcome.jobs, existing, ITERATION_METRICS_PATH)
    metrics = _read_metrics(ITERATION_METRICS_PATH) if recorded else existing

    results = [
        _check_billing(runs),
        _check_trigger_drift(runs, default_branch, now),
        _check_failure_rate(runs, job_payloads),
        _check_staleness(runs, default_branch, now),
        _check_trunk_verified(runs, default_branch, job_payloads),
        _check_iteration_speed(metrics),
    ]
    return results, default_branch, recorded, outcome.note


def _resolve_exit_code(results: list[CheckResult]) -> int:
    """Most severe failing check wins, so the notifier always leads with the worst news."""
    failures = [result.exit_code for result in results if not result.ok]
    if not failures:
        return EXIT_OK
    return min(failures)


def _emit_json(
    results: list[CheckResult],
    default_branch: str,
    exit_code: int,
    recorded: int,
    note: str | None,
) -> None:
    payload = {
        "repo": REPO,
        "default_branch": default_branch,
        "checked_at": datetime.now(UTC).isoformat(),
        "exit_code": exit_code,
        "ok": exit_code == EXIT_OK,
        "metrics_store": str(ITERATION_METRICS_PATH),
        "job_durations_recorded": recorded,
        "job_fetch_note": note,
        "checks": [asdict(result) for result in results],
    }
    print(json.dumps(payload, indent=2))


def _emit_text(results: list[CheckResult], exit_code: int, recorded: int, note: str | None) -> None:
    print(f"[OK] iteration-metrics: recorded {recorded} new CI job durations")
    # A truncated catch-up is a permanent hole in the sample, so it is [ERROR]-loud even
    # though it does not fail the run. scripts/ci_health_notify.sh alerts on a nonzero exit
    # only and never greps the log for tokens, so this cannot manufacture a false page.
    if note is not None:
        print(f"[ERROR] iteration-metrics: {note}")
    for result in results:
        print(result.verdict_line())
    if exit_code == EXIT_OK:
        print(f"[OK] ci-health: all {len(results)} checks passed")
        return
    worst = next(result for result in results if result.exit_code == exit_code and not result.ok)
    print(f"[ERROR] ci-health: {worst.classification} (exit {exit_code})")
    if worst.remediation:
        print(f"[ERROR] remediation: {worst.remediation}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"Check GitHub Actions health for {REPO} from outside CI."
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable results on stdout"
    )
    args = parser.parse_args()

    try:
        results, default_branch, recorded, note = _collect_results()
    except PreconditionError as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "repo": REPO,
                        "checked_at": datetime.now(UTC).isoformat(),
                        "exit_code": EXIT_PRECONDITION,
                        "ok": False,
                        "error": str(exc),
                    },
                    indent=2,
                )
            )
        else:
            print(f"[ERROR] precondition: {exc}", file=sys.stderr)
        return EXIT_PRECONDITION

    exit_code = _resolve_exit_code(results)
    if args.json:
        _emit_json(results, default_branch, exit_code, recorded, note)
    else:
        _emit_text(results, exit_code, recorded, note)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
