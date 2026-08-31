#!/usr/bin/env python3
"""Standing performance health check: read every perf sink that exists on THIS machine.

WHY THIS EXISTS
    Perf health runs have been opportunistic - someone remembers to look, greps a log,
    finds two Signalsmith timeouts, and the finding dies in a chat thread. This is the
    scheduled, root-cause-oriented version: one command reads every perf sink that
    actually exists on the machine it runs on, names what it found WITH the denominator
    it was counted against, and hands each finding a ROOT-CAUSE POINTER saying which
    other signal to correlate it with. It is a READER, never a writer: it mutates no
    sink, installs nothing, and calls no network.

    A finding is not an incident. The escalation contract is in
    docs/perf/health-checks.md: [ERROR] and [WARN] findings get triaged into
    docs/perf/QUEUE.md by the perf owner thread, which is where they become work.

INTERPRETER FLOOR: this file runs under ``/usr/bin/python3``, which is 3.9 on current
macOS, and it is deliberately stdlib-only, exactly like
``scripts/diagnostics/opendj_performance_probe.py``. That is what lets a launchd job run
it on a machine with no repo checkout, no venv, and no network. So: no 3.10+/3.11+
runtime syntax, whatever ruff's ``target-version`` says. ``datetime.UTC`` (3.11) is the
one the linter keeps proposing; it carries a ``noqa`` and a reason. Annotations are
exempt, because ``from __future__ import annotations`` defers them. It also imports
NOTHING from the rest of this repo - not even the constants it shares with
``scripts.ci_health_metrics`` - because an import of ``scripts.*`` needs the repo root on
sys.path, which is exactly the precondition a scheduled copy cannot promise. The
duplicated store path is the price of that property and is named as such in
``scripts/perf_health/config.py``.

    The check implementations live in the sibling package ``scripts/perf_health/`` to
    keep every file under the 600-line quality-gate limit. That package is imported here
    as a bare top-level ``perf_health``, not as ``scripts.perf_health``: when Python runs
    this file directly (``python3 scripts/perf_health_check.py``, exactly how launchd and
    ``just perf-health`` invoke it), it puts THIS file's own directory - ``scripts/`` -
    at ``sys.path[0]`` automatically, so the sibling package resolves with no sys.path
    edit, no venv, and no repo root needed. That is the same interpreter-floor property
    as before, just spread across cohesive files instead of one 900+ line one.

SINKS READ (v0 - only sinks that can exist on a machine TODAY)
    1. client-errors    webui-client-errors-*.log in BOTH default locations:
                        engine-managed <data-dir>/logs (packaged app) and the legacy
                        ~/.local/share/music-dj-tools/webui (dev daemon).
    2. iteration-metrics ~/.local/share/mdt-iteration-metrics/metrics.jsonl
    3. ci-health         ~/Library/Logs/mdt-ci-health.log + the watchdog's own checkout
    4. diagnostics-probe ~/Library/Application Support/OpenDJ Diagnostics/performance/
    5. client-perf       webui-client-perf-*.log (Q5, in flight) - reported [INFO] only

    Every check degrades to a NAMED [WARN] when its sink is absent. An absent sink is a
    coverage gap worth seeing, never a silent pass and never a crash.

SEVERITY AND EXIT CODES
    [OK]    the sink was read and says nothing is wrong
    [INFO]  a future sink, reported present/absent, never affects the exit code
    [WARN]  a finding to triage, OR a sink that is absent/stale, OR a malformed row
    [ERROR] the check cannot trust its own inputs - a sink that EXISTS but cannot be
            read (permission denied, a directory where a JSONL belongs). That is the
            quiet rot this exists to surface, so it is the only thing that fails the
            exit code. One unreadable sink is reported as its own [ERROR] and never
            blinds the other four checks.

    0   no [ERROR]
    1   at least one [ERROR]

HONEST DENOMINATORS
    Every number printed carries its window and its denominator on the same line. "3
    Signalsmith timeouts" is not a fact; "3 Signalsmith timeouts in 512 rows across 4
    days" is. A count quoted against an assumed denominator is worse than no count.

MINI-PRD
    R1 Client-error scan with root-cause pointers ................ done + ran + regression
       Scan both default log roots for the last --days days of webui-client-errors-*.log,
       count deck-load failures and Signalsmith timeouts per day, and print the evidence
       line plus a pointer naming the signal to correlate against.
       Acceptance tests:
         [if] a day's log holds 2 Signalsmith timeout rows
              [then] the day count is 2, against that day's total row count, and each
              evidence line carries the worklet-ack / xrun pointer (*)
         [if] a deck-load failure row carries stage_* context (PR #539)
              [then] the heaviest stage is rendered on the evidence line (*)
         [if] neither log root exists
              [then] [WARN] naming both paths, never a crash and never a silent pass (*)
         [if] a log line is not valid JSON
              [then] it is counted as malformed and reported with its file and line
              number, and the scan continues over the rest of the file (*)
    R2 Iteration-metrics freshness ............................... done + ran + regression
       Acceptance tests:
         [if] the store is absent
              [then] [WARN] naming the path (*)
         [if] the store holds 0 rows inside the 48h window
              [then] [WARN] carrying the JOB_FETCH_RUN_COUNT burst caveat (*)
         [if] the store holds rows inside the window
              [then] [OK] quoting rows-in-window over total rows (*)
    R3 CI-health watchdog liveness ............................... done + ran + regression
       Acceptance tests:
         [if] the newest log timestamp is older than WATCHDOG_MAX_RUN_AGE_HOURS
              [then] [WARN] naming the age and the schedule it was expected on (*)
         [if] the watchdog checkout's last commit is older than 7 days
              [then] [WARN] - self-update landed in PR #538, so a stale checkout means
              the self-update is not running (*)
         [if] the log exists but holds no parseable timestamp
              [then] [WARN] naming the line count it read (*)
    R4 Diagnostics-probe presence ................................ done + ran + regression
       Acceptance tests:
         [if] the probe output directory is absent
              [then] [WARN] 'probe not installed', pointing at the install runbook (*)
         [if] the newest sample JSONL is older than PROBE_MAX_SAMPLE_AGE_HOURS
              [then] [WARN] - a probe under launchd samples every 15s, so a day-old
              newest sample means it stopped (*)
    R5 Machine-readable output ................................... done + ran + regression
       Acceptance tests:
         [if] --json is passed
              [then] stdout parses as JSON with keys checked_at, exit_code, checks (*)
         [if] --json is passed
              [then] every check object carries its window and its findings (*)
    R6 Unreadable sinks are loud ................................. done + ran + regression
       Acceptance tests:
         [if] a sink exists but reading it raises OSError
              [then] that check alone is [ERROR], the other checks still run, exit 1 (*)
         [if] a JSONL sink path is a directory
              [then] [ERROR] naming the path, never a traceback (*)
    R7 Remediation of what it finds .............................. out of scope
       This check DETECTS and POINTS. It does not restart probes, install launchd jobs,
       or edit the queue. Triage is a human/owner-thread decision, per
       docs/perf/health-checks.md.

-Claude
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime

from perf_health.checks_ci_watchdog import check_ci_health_watchdog
from perf_health.checks_client_errors import check_client_errors
from perf_health.checks_client_perf import check_client_perf_sink
from perf_health.checks_diagnostics_probe import check_diagnostics_probe
from perf_health.checks_iteration_metrics import check_iteration_metrics
from perf_health.config import (
    CLIENT_LOG_DIRS,
    DEFAULT_ERROR_LOG_DAYS,
    EXIT_ERROR,
    EXIT_OK,
    ITERATION_METRICS_PATH,
    MAX_EVIDENCE_ROWS,
    PROBE_DIR,
    QUEUE_DOC,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_OK,
    SEVERITY_WARN,
    WATCHDOG_LOG_PATH,
    WATCHDOG_REPO_PATH,
)
from perf_health.models import CheckResult, PreconditionError
from perf_health.util import _now, _window_label

# ----- orchestration -----------------------------------------------------------------


def _guarded(name: str, run: Callable[[], CheckResult]) -> CheckResult:
    """Run one check, turning an unreadable sink into a named [ERROR] instead of a crash.

    Only OSError and PreconditionError are caught, and both mean the same thing: a sink
    that exists but cannot be trusted. Anything else is a bug in this file and must
    surface as a traceback rather than be laundered into a verdict.
    """
    try:
        return run()
    except (PreconditionError, OSError) as exc:
        return CheckResult(
            check=name,
            severity=SEVERITY_ERROR,
            classification="unreadable-sink",
            detail=f"sink exists but could not be read: {exc}",
            window="n/a",
            remediation="fix the path's permissions or type, then re-run `just perf-health`",
        )


def collect_results(days: int, now: datetime) -> list[CheckResult]:
    """Every check, in the order a reader should think about them: findings, then sinks.

    This is the ONLY function that knows the machine's layout. The checks take their
    sinks as arguments so a test can point them at a tmp dir without patching globals.
    """
    return [
        _guarded("client-errors", lambda: check_client_errors(CLIENT_LOG_DIRS, days, now)),
        _guarded("iteration-metrics", lambda: check_iteration_metrics(ITERATION_METRICS_PATH, now)),
        _guarded(
            "ci-health-watchdog",
            lambda: check_ci_health_watchdog(WATCHDOG_LOG_PATH, WATCHDOG_REPO_PATH, now),
        ),
        _guarded("diagnostics-probe", lambda: check_diagnostics_probe(PROBE_DIR, now)),
        _guarded("client-perf-sink", lambda: check_client_perf_sink(CLIENT_LOG_DIRS, days, now)),
    ]


def resolve_exit_code(results: list[CheckResult]) -> int:
    return EXIT_ERROR if any(result.is_error for result in results) else EXIT_OK


SEVERITIES = (SEVERITY_OK, SEVERITY_INFO, SEVERITY_WARN, SEVERITY_ERROR)


def _headline(results: list[CheckResult], days: int) -> str:
    counts = dict.fromkeys(SEVERITIES, 0)
    for result in results:
        counts[result.severity] += 1
    if counts[SEVERITY_ERROR]:
        marker = SEVERITY_ERROR
    elif counts[SEVERITY_WARN]:
        marker = SEVERITY_WARN
    else:
        marker = SEVERITY_OK
    return (
        f"[{marker}] perf-health: {len(results)} checks over the {_window_label(days)} - "
        f"{counts[SEVERITY_ERROR]} error, {counts[SEVERITY_WARN]} warn, "
        f"{counts[SEVERITY_OK]} ok, {counts[SEVERITY_INFO]} info"
    )


def _emit_text(results: list[CheckResult], days: int) -> None:
    print(_headline(results, days))
    for result in results:
        print(result.verdict_line())
        for row in result.breakdown:
            print(f"      {row}")
        for finding in result.findings[:MAX_EVIDENCE_ROWS]:
            for line in finding.evidence_lines():
                print(line)
        hidden = len(result.findings) - MAX_EVIDENCE_ROWS
        if hidden > 0:
            print(f"      ... {hidden} more of {len(result.findings)} findings not shown")
        if result.remediation:
            print(f"      remediation: {result.remediation}")
    triage = [r.check for r in results if r.severity in (SEVERITY_WARN, SEVERITY_ERROR)]
    if triage:
        print(
            f"[WARN] triage: {', '.join(triage)} -> escalate into {QUEUE_DOC}, "
            "contract in docs/perf/health-checks.md"
        )


def _emit_json(results: list[CheckResult], days: int, exit_code: int, now: datetime) -> None:
    payload = {
        "checked_at": now.isoformat(),
        "window_days": days,
        "exit_code": exit_code,
        "ok": exit_code == EXIT_OK,
        "queue_doc": QUEUE_DOC,
        "checks": [asdict(result) for result in results],
    }
    print(json.dumps(payload, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read every Open DJ performance sink that exists on this machine."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_ERROR_LOG_DAYS,
        help=f"how many days of daily logs to scan (default {DEFAULT_ERROR_LOG_DAYS})",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable results on stdout"
    )
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")

    now = _now()
    results = collect_results(args.days, now)
    exit_code = resolve_exit_code(results)
    if args.json:
        _emit_json(results, args.days, exit_code, now)
    else:
        _emit_text(results, args.days)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
