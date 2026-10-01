#!/usr/bin/env python3
"""The one rule for a GitHub Actions run that reports `in_progress` and never ends.

On Thu 1 Oct 2026 two CI runs (36802069871 and 36803336485) read `in_progress` for 17h+
while cancel and force-cancel both returned HTTP 409 "not in progress". GitHub had lost
them: no job was running, and nothing would ever complete them. A bookkeeping pass that
waits for every old in-flight run (the cost guard's and stable evidence's census) or
fails on a refused cancel (the trunk-tip-only closed-PR sweep) is then red on every pass
forever, and the work behind it stops with it.

The rule: a run whose status is exactly `in_progress` and whose `created_at` is more than
PHANTOM_AFTER_HOURS before now is a PHANTOM. A caller names it by id (log line and job
summary) and skips it: never silently dropped, never failing forever. Every other run is
untouched by this rule:

- `queued`, `waiting`, `pending` and `requested` runs are never phantoms. A job queued on
  a saturated pool is bounded by no `timeout-minutes` (Codex P1 on #3844), so age says
  nothing about whether a queued run is alive.
- A recent `in_progress` run is alive and keeps holding whatever it held.

PHANTOM_AFTER_HOURS is pinned against the repository's own workflows by
tests/scripts/test_ci_phantom_runs.py: it must exceed the longest `needs` chain of
`timeout-minutes` in any workflow, the longest lookback a census uses, and the longest
creation-to-completion span measured on a live run (6.0 h, Mon 28 Sep 2026, cited in
ci-cost-guard.yml). 12 h is twice that measured span.

-Claude
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

PHANTOM_AFTER_HOURS = 12
PHANTOM_STATUS = "in_progress"


def _created(run: Mapping[str, Any]) -> datetime:
    value = run.get("created_at")
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        # Refused, not defaulted: a run with no creation time cannot be aged, and guessing
        # either way is a verdict from a measurement that did not happen.
        raise ValueError(f"run {run.get('id')!r} has no created_at: {value!r}")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_phantom(run: Mapping[str, Any], now: datetime, *, after: timedelta | None = None) -> bool:
    """True when `run` is `in_progress` and was created more than `after` before `now`."""
    if run.get("status") != PHANTOM_STATUS:
        return False
    threshold = after if after is not None else timedelta(hours=PHANTOM_AFTER_HOURS)
    return now - _created(run) > threshold


def split_phantoms(
    runs: Iterable[Mapping[str, Any]], now: datetime, *, after: timedelta | None = None
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """(live, phantom), each in input order."""
    live: list[Mapping[str, Any]] = []
    phantom: list[Mapping[str, Any]] = []
    for run in runs:
        (phantom if is_phantom(run, now, after=after) else live).append(run)
    return live, phantom


def phantom_line(run: Mapping[str, Any], now: datetime, *, caller: str) -> str:
    """One greppable line naming a skipped phantom by id."""
    age_hours = (now - _created(run)).total_seconds() / 3600
    return (
        f"phantom-run-skipped caller={caller} run_id={run.get('id')} "
        f"workflow={run.get('name')!r} status={run.get('status')} "
        f"created_at={run.get('created_at')} age_hours={age_hours:.1f} "
        f"threshold_hours={PHANTOM_AFTER_HOURS}"
    )


def write_step_summary(lines: list[str], *, caller: str) -> None:
    """Append the phantom lines to the job summary when running inside Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not path or not lines:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(
            f"### {caller}: {len(lines)} phantom run(s) skipped\n\n"
            f"Each was `{PHANTOM_STATUS}` and created more than {PHANTOM_AFTER_HOURS} h "
            "ago (scripts/ci_phantom_runs.py). It was skipped, not waited on.\n\n"
        )
        for line in lines:
            handle.write(f"- `{line}`\n")
        handle.write("\n")
