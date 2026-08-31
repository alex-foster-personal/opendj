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
NOTHING from this repo - not even the constants it shares with
``scripts.ci_health_metrics`` - because an import of ``scripts.*`` needs the repo root on
sys.path, which is exactly the precondition a scheduled copy cannot promise. The
duplicated store path is the price of that property and is named as such below.

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
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ----- configuration ---------------------------------------------------------------

HOME = Path.home()

# macOS derives the packaged app's data dir from its bundle identifier, so the engine's
# logs move when the identifier does. Same candidate list, same reason, as
# scripts/diagnostics/opendj_performance_probe.py after the OPS-08 bake-off.
BUNDLE_IDS = ("com.opendj.desktop", "com.opendj.desktop.lane-b")
ENGINE_LOG_DIRS = tuple(
    HOME / "Library" / "Application Support" / bundle_id / "logs" for bundle_id in BUNDLE_IDS
)
# apps/webui/server/client_logs.DEFAULT_LOG_DIR: where the dev daemon writes.
LEGACY_LOG_DIR = HOME / ".local" / "share" / "music-dj-tools" / "webui"
CLIENT_LOG_DIRS = (*ENGINE_LOG_DIRS, LEGACY_LOG_DIR)

CLIENT_ERROR_PREFIX = "webui-client-errors"
# Q5 in docs/perf/QUEUE.md. Probed leniently so the check reports the day it lands.
CLIENT_PERF_PREFIX = "webui-client-perf"
LOG_DATE_RE = re.compile(r"-(\d{4}-\d{2}-\d{2})\.log$")

DEFAULT_ERROR_LOG_DAYS = 7
# Enough rows to see a pattern, few enough that one bad day cannot bury the summary. The
# full count is always printed, so truncation never hides the denominator.
MAX_EVIDENCE_ROWS = 5

# DUPLICATED, deliberately: scripts/ci_health_metrics.ITERATION_METRICS_PATH holds the
# same value. Importing it would put the repo root on the required-precondition list for
# a check that must run from a scheduled copy. One constant, two readers, one comment.
ITERATION_METRICS_PATH = HOME / ".local" / "share" / "mdt-iteration-metrics" / "metrics.jsonl"
ITERATION_METRICS_WINDOW_HOURS = 48

WATCHDOG_LOG_PATH = HOME / "Library" / "Logs" / "mdt-ci-health.log"
WATCHDOG_REPO_PATH = HOME / ".local" / "share" / "mdt-ci-health" / "repo"
# The watchdog runs 4-hourly, so 8h is two consecutive missed runs, not one late one.
WATCHDOG_MAX_RUN_AGE_HOURS = 8.0
# Self-update (PR #538) pulls --ff-only before every run. A checkout that has not moved
# in a week means the pull is failing, and the watchdog is running week-old checks.
WATCHDOG_MAX_CHECKOUT_AGE_DAYS = 7.0
WATCHDOG_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s")
WATCHDOG_START_MARKER = "starting check"

PROBE_DIR = HOME / "Library" / "Application Support" / "OpenDJ Diagnostics" / "performance"
# The probe samples every 15 seconds under launchd, so a newest sample older than a day
# means it stopped, not that it is quiet.
PROBE_MAX_SAMPLE_AGE_HOURS = 24.0
PROBE_INSTALL_DOC = "docs/perf/diagnostics-probe.md"

QUEUE_DOC = "docs/perf/QUEUE.md"

SEVERITY_OK = "OK"
SEVERITY_INFO = "INFO"
SEVERITY_WARN = "WARN"
SEVERITY_ERROR = "ERROR"

EXIT_OK = 0
EXIT_ERROR = 1

# ----- signatures ------------------------------------------------------------------

# Each signature is counted separately so the headline number is decomposable: "4
# deck-load failures" is only useful if you can see it was 3 broken links and 1 real
# engine failure. Order is irrelevant; a row is attributed to the first match.
SIGNALSMITH_SIGNATURES = (
    ("signalsmith-timeout", re.compile(r"Signalsmith\b.*\btimed out\b", re.IGNORECASE)),
)
DECK_LOAD_SIGNATURES = (
    ("engine-load-failed", re.compile(r"Deck \d+ load failed")),
    ("drop-load-failed", re.compile(r"drop load failed")),
    # 'Performance command failed - Error: load: deck 4 must be fully stopped ...'
    ("load-precondition", re.compile(r"\bload: deck \d+")),
    ("audio-missing", re.compile(r"cannot load: ")),
)
# PR #539 stamps source='deck-load' onto the failure context, which is the channel that
# does not depend on message wording. Message regexes stay as the pre-#539 fallback.
DECK_LOAD_CONTEXT_SOURCE = "deck-load"
STAGE_PREFIX = "stage_"

SIGNALSMITH_POINTER = (
    "check worklet-ack p95 rows and xrun events around that time (shipped by PR #550), "
    "and whether decodeMix dominated the preceding deck-load"
)
DECK_LOAD_POINTER = (
    "read the stage_* fields for WHERE the load died (PR #539 stamps them); a dominant "
    "stage_decodeMix or stage_stemProcessorCreate is Q6/Q7 in " + QUEUE_DOC + ", not a new bug"
)

# ----- data ------------------------------------------------------------------------


class PreconditionError(RuntimeError):
    """Raised when a path exists but is not the kind of thing it must be. Never swallowed."""


@dataclass
class Finding:
    """One evidence row plus the pointer saying which other signal to correlate it with."""

    when: str
    signature: str
    message: str
    context: str
    pointer: str

    def evidence_lines(self) -> list[str]:
        head = f"  {self.when}  {self.signature}: {self.message}"
        lines = [head]
        if self.context:
            lines.append(f"      context: {self.context}")
        lines.append(f"      -> {self.pointer}")
        return lines


@dataclass
class CheckResult:
    """Verdict for one named sink, carrying its own window and denominator."""

    check: str
    severity: str
    classification: str
    detail: str
    window: str
    sources: list[str] = field(default_factory=list)
    breakdown: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    remediation: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity == SEVERITY_ERROR

    def verdict_line(self) -> str:
        return f"[{self.severity}] {self.check}: {self.detail}"


# ----- shared helpers --------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11, floor is 3.9


def _age_hours(moment: datetime, now: datetime) -> float:
    return (now - moment).total_seconds() / 3600.0


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)  # noqa: UP017


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _window_label(days: int) -> str:
    return f"last {_plural(days, 'day')}"


def _describe_age(hours: float) -> str:
    if hours < 48:
        return f"{hours:.1f}h old"
    return f"{hours / 24:.1f}d old"


def _read_lines(path: Path) -> list[str]:
    """Read a JSONL sink, failing loudly if the path is not a readable file.

    An unreadable sink is the quiet rot this check exists to surface, so the OSError is
    turned into a named [ERROR] by the caller rather than being swallowed into a pass.
    """
    if path.is_dir():
        raise PreconditionError(f"{path} is a directory, expected a JSONL file")
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _log_files(
    log_dirs: tuple[Path, ...], prefix: str, days: int, now: datetime
) -> tuple[list[Path], list[Path]]:
    """Daily logs inside the window, plus the log roots that exist at all.

    The date comes from the filename, which is how apps/webui/server/client_logs.py names
    them, so a file's day is known without reading it.
    """
    cutoff = (now - timedelta(days=days)).date()
    matched: list[Path] = []
    present_roots: list[Path] = []
    for directory in log_dirs:
        if not directory.is_dir():
            continue
        present_roots.append(directory)
        for path in sorted(directory.glob(f"{prefix}-*.log")):
            stamp = LOG_DATE_RE.search(path.name)
            if stamp is None:
                continue
            # A log FILENAME carries a bare local date with no zone to honor; only the
            # .date() is used, so attaching a zone here would invent precision.
            day = datetime.strptime(stamp.group(1), "%Y-%m-%d").date()  # noqa: DTZ007
            if day >= cutoff:
                matched.append(path)
    return matched, present_roots


# ----- check 1: client-error JSONLs --------------------------------------------------


def _classify(record: dict) -> tuple[str, str] | None:
    """(family, signature) for a row worth reporting, or None.

    Families are kept apart because they point at different evidence: a Signalsmith
    timeout is a worklet/xrun question, a deck-load failure is a stage-timing question.
    """
    message = str(record.get("message", ""))
    for name, pattern in SIGNALSMITH_SIGNATURES:
        if pattern.search(message):
            return "signalsmith", name
    context = record.get("context")
    if isinstance(context, dict) and context.get("source") == DECK_LOAD_CONTEXT_SOURCE:
        return "deck-load", "deck-load-context"
    for name, pattern in DECK_LOAD_SIGNATURES:
        if pattern.search(message):
            return "deck-load", name
    return None


def _render_context(record: dict) -> str:
    """Stage timings heaviest-first, then the rest of the context.

    PR #539 flattens a failed load's stage map into ``stage_<name>`` keys, and the
    heaviest stage is the whole reason the context is attached, so it leads.
    """
    context = record.get("context")
    if not isinstance(context, dict) or not context:
        return ""
    stages = {k: v for k, v in context.items() if k.startswith(STAGE_PREFIX)}
    numeric = {k: v for k, v in stages.items() if isinstance(v, (int, float))}
    ordered = sorted(numeric.items(), key=lambda item: item[1], reverse=True)
    ordered += sorted((k, v) for k, v in stages.items() if k not in numeric)
    ordered += sorted((k, v) for k, v in context.items() if not k.startswith(STAGE_PREFIX))
    return " ".join(f"{key}={value}" for key, value in ordered)


def _finding(record: dict, family: str, signature: str) -> Finding:
    when = str(record.get("received_at") or record.get("client_timestamp") or "unknown-time")
    pointer = SIGNALSMITH_POINTER if family == "signalsmith" else DECK_LOAD_POINTER
    label = "Signalsmith timeout" if family == "signalsmith" else "deck-load failure"
    return Finding(
        when=when,
        signature=signature,
        message=str(record.get("message", "")).replace("\n", " ")[:240],
        context=_render_context(record),
        pointer=f"{label} at {when}: {pointer}",
    )


@dataclass
class _ScanTally:
    """Running counts for one client-error scan, so every printed number has a source."""

    rows: int = 0
    malformed: list[str] = field(default_factory=list)
    per_day: dict = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    def note_day(self, day: str, family: str) -> None:
        bucket = self.per_day.setdefault(day, {"rows": 0, "signalsmith": 0, "deck-load": 0})
        bucket[family] += 1


def _scan_client_errors(paths: list[Path]) -> _ScanTally:
    tally = _ScanTally()
    for path in paths:
        stamp = LOG_DATE_RE.search(path.name)
        day = stamp.group(1) if stamp else "unknown-day"
        bucket = tally.per_day.setdefault(day, {"rows": 0, "signalsmith": 0, "deck-load": 0})
        for number, line in enumerate(_read_lines(path), start=1):
            if not line.strip():
                continue
            tally.rows += 1
            bucket["rows"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                tally.malformed.append(f"{path}:{number} ({exc.msg})")
                continue
            if not isinstance(record, dict):
                kind = type(record).__name__
                tally.malformed.append(f"{path}:{number} (row is {kind}, not an object)")
                continue
            hit = _classify(record)
            if hit is None:
                continue
            family, signature = hit
            tally.note_day(day, family)
            tally.findings.append(_finding(record, family, signature))
    return tally


def _client_error_detail(tally: _ScanTally, paths: list[Path], days: int) -> str:
    signalsmith = sum(day["signalsmith"] for day in tally.per_day.values())
    deck_load = sum(day["deck-load"] for day in tally.per_day.values())
    return (
        f"{deck_load} deck-load failures and {signalsmith} Signalsmith timeouts "
        f"in {_plural(tally.rows, 'row')} across {_plural(len(paths), 'daily log')} "
        f"({_window_label(days)}), {_plural(len(tally.malformed), 'malformed row')}"
    )


def _client_error_breakdown(tally: _ScanTally) -> list[str]:
    """Per-day counts against that day's own row count, because a rate needs its day."""
    return [
        f"{day}: {counts['deck-load']} deck-load, {counts['signalsmith']} signalsmith "
        f"in {_plural(counts['rows'], 'row')}"
        for day, counts in sorted(tally.per_day.items())
    ]


def check_client_errors(
    log_dirs: tuple[Path, ...], days: int, now: datetime
) -> CheckResult:
    window = _window_label(days)
    paths, present_roots = _log_files(log_dirs, CLIENT_ERROR_PREFIX, days, now)
    if not present_roots:
        return CheckResult(
            check="client-errors",
            severity=SEVERITY_WARN,
            classification="sink-absent",
            detail="no client-error log root exists on this machine, so 0 rows were readable",
            window=window,
            remediation=(
                "none of the expected roots exist: "
                + ", ".join(str(directory) for directory in log_dirs)
            ),
        )
    if not paths:
        roots = ", ".join(str(root) for root in present_roots)
        return CheckResult(
            check="client-errors",
            severity=SEVERITY_WARN,
            classification="no-logs-in-window",
            detail=f"0 daily logs inside the {window} across {len(present_roots)} roots",
            window=window,
            sources=[str(root) for root in present_roots],
            remediation=f"the app has not written a client-error log in the {window} ({roots})",
        )

    tally = _scan_client_errors(paths)
    detail = _client_error_detail(tally, paths, days)
    severity = SEVERITY_WARN if (tally.findings or tally.malformed) else SEVERITY_OK
    classification = "healthy"
    if tally.findings:
        classification = "findings"
    elif tally.malformed:
        classification = "malformed-rows"
    return CheckResult(
        check="client-errors",
        severity=severity,
        classification=classification,
        detail=detail,
        window=window,
        sources=[str(path) for path in paths],
        breakdown=_client_error_breakdown(tally),
        findings=tally.findings,
        remediation=(
            "; ".join(tally.malformed[:MAX_EVIDENCE_ROWS]) if tally.malformed else ""
        ),
    )


# ----- check 2: iteration metrics ----------------------------------------------------

ITERATION_BURST_CAVEAT = (
    "`just ci-health` records only the newest JOB_FETCH_RUN_COUNT=10 runs per 4-hourly "
    "poll, so a CI burst larger than that between polls is never recorded - an empty "
    "window with recent CI activity means the poll is not running, or the burst outran it"
)


def _count_recent_rows(lines: list[str], now: datetime) -> tuple[int, int, list[str]]:
    """(rows in window, total rows, malformed descriptions) for the metrics store."""
    cutoff = now - timedelta(hours=ITERATION_METRICS_WINDOW_HOURS)
    recent = 0
    total = 0
    malformed: list[str] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        total += 1
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            malformed.append(f"line {number} ({exc.msg})")
            continue
        stamp = record.get("ts") if isinstance(record, dict) else None
        if not isinstance(stamp, str):
            malformed.append(f"line {number} (no ts field)")
            continue
        try:
            moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            malformed.append(f"line {number} (unparseable ts {stamp!r})")
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)  # noqa: UP017
        if moment >= cutoff:
            recent += 1
    return recent, total, malformed


def check_iteration_metrics(store: Path, now: datetime) -> CheckResult:
    window = f"last {ITERATION_METRICS_WINDOW_HOURS}h"
    if not store.exists():
        return CheckResult(
            check="iteration-metrics",
            severity=SEVERITY_WARN,
            classification="sink-absent",
            detail=f"store absent at {store}, so 0 of 0 rows were readable",
            window=window,
            remediation="run `just ci-health` or any wrapped `just` recipe once to create it",
        )
    age = _age_hours(_mtime(store), now)
    recent, total, malformed = _count_recent_rows(_read_lines(store), now)
    detail = (
        f"{recent} rows appended in the last {ITERATION_METRICS_WINDOW_HOURS}h of "
        f"{total} rows in the store, newest write {_describe_age(age)}, "
        f"{len(malformed)} malformed rows"
    )
    if recent == 0:
        return CheckResult(
            check="iteration-metrics",
            severity=SEVERITY_WARN,
            classification="no-recent-rows",
            detail=detail,
            window=window,
            sources=[str(store)],
            remediation=ITERATION_BURST_CAVEAT,
        )
    if malformed:
        return CheckResult(
            check="iteration-metrics",
            severity=SEVERITY_WARN,
            classification="malformed-rows",
            detail=detail,
            window=window,
            sources=[str(store)],
            remediation="; ".join(malformed[:MAX_EVIDENCE_ROWS]),
        )
    return CheckResult(
        check="iteration-metrics",
        severity=SEVERITY_OK,
        classification="healthy",
        detail=detail,
        window=window,
        sources=[str(store)],
    )


# ----- check 3: CI-health watchdog ---------------------------------------------------


def _newest_watchdog_run(lines: list[str]) -> tuple[datetime | None, str]:
    """(newest parseable timestamp, last verdict line) from the watchdog log.

    The 'starting check' line is skipped when picking the verdict: it is stamped before
    the run has decided anything, so reporting it would say a completed run was in
    progress. Its timestamp still counts as liveness.
    """
    newest = None
    verdict = ""
    for line in lines:
        stamp = WATCHDOG_LOG_TS_RE.match(line)
        if stamp is None:
            continue
        moment = datetime.strptime(stamp.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc  # noqa: UP017
        )
        if newest is None or moment > newest:
            newest = moment
        if WATCHDOG_START_MARKER not in line:
            verdict = line.strip()
    return newest, verdict


def _checkout_age_days(repo: Path, now: datetime) -> tuple[float | None, str]:
    """(age in days of the watchdog checkout's last commit, explanation when unknown)."""
    if not (repo / ".git").exists():
        return None, f"no checkout at {repo}"
    git = shutil.which("git")
    if git is None:
        return None, "git not on PATH, checkout freshness unknown"
    # Fixed argv and no shell, so there is no injection surface. check=False on purpose:
    # the returncode is inspected below so the caller can explain WHY the age is unknown
    # instead of raising an opaque CalledProcessError over a non-fatal detail.
    completed = subprocess.run(
        [git, "-C", str(repo), "log", "-1", "--format=%cI"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        return None, f"git log failed in {repo}: {completed.stderr.strip()}"
    stamp = completed.stdout.strip()
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None, f"unparseable commit date {stamp!r}"
    return (now - moment).total_seconds() / 86400.0, ""


def check_ci_health_watchdog(log_path: Path, repo: Path, now: datetime) -> CheckResult:
    window = f"last run within {WATCHDOG_MAX_RUN_AGE_HOURS:.0f}h"
    if not log_path.exists():
        return CheckResult(
            check="ci-health-watchdog",
            severity=SEVERITY_WARN,
            classification="sink-absent",
            detail=f"watchdog log absent at {log_path}, so 0 runs were readable",
            window=window,
            remediation=(
                "install the launchd job, see the `ci-health` recipe comment in the justfile"
            ),
        )
    lines = _read_lines(log_path)
    newest, verdict = _newest_watchdog_run(lines)
    checkout_days, checkout_note = _checkout_age_days(repo, now)
    checkout_text = (
        f"checkout last commit {checkout_days:.1f}d old"
        if checkout_days is not None
        else f"checkout age unknown ({checkout_note})"
    )
    if newest is None:
        return CheckResult(
            check="ci-health-watchdog",
            severity=SEVERITY_WARN,
            classification="unparseable-log",
            detail=f"no parseable timestamp in {len(lines)} log lines, {checkout_text}",
            window=window,
            sources=[str(log_path)],
            remediation=f"expected lines starting {WATCHDOG_LOG_TS_RE.pattern!r}",
        )
    age = _age_hours(newest, now)
    detail = (
        f"last run {_describe_age(age)} over {len(lines)} log lines, {checkout_text}; "
        f"last status: {verdict or 'none recorded'}"
    )
    if age > WATCHDOG_MAX_RUN_AGE_HOURS:
        return CheckResult(
            check="ci-health-watchdog",
            severity=SEVERITY_WARN,
            classification="stale-run",
            detail=detail,
            window=window,
            sources=[str(log_path)],
            remediation=(
                f"the watchdog runs 4-hourly; {_describe_age(age)} is at least two missed "
                "runs. Check `launchctl print gui/$(id -u)/com.maintainer.mdt-ci-health`"
            ),
        )
    if checkout_days is not None and checkout_days > WATCHDOG_MAX_CHECKOUT_AGE_DAYS:
        return CheckResult(
            check="ci-health-watchdog",
            severity=SEVERITY_WARN,
            classification="stale-checkout",
            detail=detail,
            window=window,
            sources=[str(log_path), str(repo)],
            remediation=(
                f"self-update (PR #538) pulls --ff-only before every run, so a "
                f"{checkout_days:.1f}d-old checkout means the pull is failing and the "
                "watchdog is running week-old checks"
            ),
        )
    if checkout_days is None:
        return CheckResult(
            check="ci-health-watchdog",
            severity=SEVERITY_WARN,
            classification="checkout-unknown",
            detail=detail,
            window=window,
            sources=[str(log_path)],
            remediation=checkout_note,
        )
    return CheckResult(
        check="ci-health-watchdog",
        severity=SEVERITY_OK,
        classification="healthy",
        detail=detail,
        window=window,
        sources=[str(log_path), str(repo)],
    )


# ----- check 4: diagnostics probe ----------------------------------------------------


def check_diagnostics_probe(probe_dir: Path, now: datetime) -> CheckResult:
    window = f"newest sample within {PROBE_MAX_SAMPLE_AGE_HOURS:.0f}h"
    if not probe_dir.is_dir():
        return CheckResult(
            check="diagnostics-probe",
            severity=SEVERITY_WARN,
            classification="not-installed",
            detail=f"probe not installed - no directory at {probe_dir}, so 0 samples exist",
            window=window,
            remediation=f"probe not installed - see {PROBE_INSTALL_DOC} once PR #548 merges",
        )
    samples = sorted(probe_dir.glob("*.jsonl"))
    if not samples:
        return CheckResult(
            check="diagnostics-probe",
            severity=SEVERITY_WARN,
            classification="no-samples",
            detail=f"probe directory exists but holds 0 *.jsonl samples ({probe_dir})",
            window=window,
            sources=[str(probe_dir)],
            remediation=f"probe installed but never sampled - see {PROBE_INSTALL_DOC}",
        )
    newest = max(samples, key=lambda path: path.stat().st_mtime)
    age = _age_hours(_mtime(newest), now)
    detail = (
        f"newest of {len(samples)} sample files is {_describe_age(age)} ({newest.name})"
    )
    if age > PROBE_MAX_SAMPLE_AGE_HOURS:
        return CheckResult(
            check="diagnostics-probe",
            severity=SEVERITY_WARN,
            classification="stale-samples",
            detail=detail,
            window=window,
            sources=[str(newest)],
            remediation=(
                "the probe samples every 15s under launchd, so a stale newest sample "
                f"means it stopped - see {PROBE_INSTALL_DOC}"
            ),
        )
    return CheckResult(
        check="diagnostics-probe",
        severity=SEVERITY_OK,
        classification="healthy",
        detail=detail,
        window=window,
        sources=[str(newest)],
    )


# ----- check 5: future client-perf sink ----------------------------------------------


def check_client_perf_sink(
    log_dirs: tuple[Path, ...], days: int, now: datetime
) -> CheckResult:
    window = _window_label(days)
    paths, _ = _log_files(log_dirs, CLIENT_PERF_PREFIX, days, now)
    if not paths:
        return CheckResult(
            check="client-perf-sink",
            severity=SEVERITY_INFO,
            classification="absent",
            detail=(
                f"0 {CLIENT_PERF_PREFIX}-*.log files in the {window} - Q5 in "
                f"{QUEUE_DOC} is the PR that creates this sink"
            ),
            window=window,
        )
    return CheckResult(
        check="client-perf-sink",
        severity=SEVERITY_INFO,
        classification="present",
        detail=f"{len(paths)} {CLIENT_PERF_PREFIX}-*.log files in the {window}",
        window=window,
        sources=[str(path) for path in paths],
    )


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
