"""Check 3: CI-health watchdog liveness (``mdt-ci-health`` launchd job)."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    SEVERITY_OK,
    SEVERITY_WARN,
    WATCHDOG_LOG_TS_RE,
    WATCHDOG_MAX_CHECKOUT_AGE_DAYS,
    WATCHDOG_MAX_RUN_AGE_HOURS,
    WATCHDOG_START_MARKER,
)
from .models import CheckResult
from .util import _age_hours, _describe_age, _read_lines


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
                "runs. Check `launchctl print gui/$(id -u)/com.YOU.mdt-ci-health`"
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
