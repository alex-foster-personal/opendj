"""Check 5: future client-perf sink presence (Q5 in docs/perf/QUEUE.md, in flight)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .config import CLIENT_PERF_PREFIX, QUEUE_DOC, SEVERITY_INFO
from .models import CheckResult
from .util import _log_files, _window_label


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
