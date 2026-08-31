"""Check 2: iteration-metrics store freshness (``just ci-health``'s own JSONL)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ITERATION_METRICS_WINDOW_HOURS, MAX_EVIDENCE_ROWS, SEVERITY_OK, SEVERITY_WARN
from .models import CheckResult
from .util import _age_hours, _describe_age, _mtime, _read_lines

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
