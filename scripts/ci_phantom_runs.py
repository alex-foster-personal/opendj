#!/usr/bin/env python3
"""The one rule for a GitHub Actions run that reports `in_progress` and never ends.

On Thu 1 Oct 2026 two CI runs (36802069871 and 36803336485) read `in_progress` for 17h+
while cancel and force-cancel both returned HTTP 409 "not in progress". GitHub had lost
them: no job was running, and nothing would ever complete them. A bookkeeping pass that
waits for every old in-flight run (the cost guard's and stable evidence's census) or
fails on a refused cancel (the trunk-tip-only closed-PR sweep) is then red on every pass
forever, and the work behind it stops with it.

The rule: a run whose status is exactly `in_progress`, whose LAST ACTIVITY (`updated_at`)
is more than PHANTOM_AFTER_HOURS before now, and none of whose jobs is waiting for a
runner is a PHANTOM. A caller names it by id (log line and job summary) and skips it:
never silently dropped, never failing forever. Every other run is untouched by this rule:

- `queued`, `waiting`, `pending` and `requested` runs are never phantoms. A job queued on
  a saturated pool is bounded by no `timeout-minutes` (Codex P1 on #3844), so age says
  nothing about whether a queued run is alive.
- Age is measured from `updated_at`, never from `created_at` (Sol P1 on #4858): created_at
  includes every hour the run spent queued, so a run queued for 14 h and executing for one
  minute would read as 14 h old. `updated_at` moves on every job transition, so it is the
  time since the run last did anything.
- An `in_progress` run with a job still `queued`, `waiting`, `pending` or `requested` is
  alive but idle behind a saturated pool, which no `timeout-minutes` bounds. Its jobs are
  read (only for a run already stale, so the extra API call is rare) and any waiting job
  keeps it live. The two lost runs of Thu 1 Oct 2026 show the orphan signature: one had all
  17 of its jobs `completed`, the other 3 `completed` and 11 `in_progress`, each with its
  run `updated_at` 19 h old; neither had a waiting job.
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
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, TypeVar

T = TypeVar("T")

PHANTOM_AFTER_HOURS = 12
PHANTOM_STATUS = "in_progress"
# A job in any of these is waiting for a runner or a gate, not lost: no timeout bounds it.
WAITING_JOB_STATUSES = frozenset({"queued", "waiting", "pending", "requested"})

# Reads one run's jobs by run id. Supplied by each caller (the census over urllib, the sweep
# over `gh`) and called only for a run that is already `in_progress` and stale.
JobsOf = Callable[[int], Iterable[Mapping[str, Any]]]


def _last_activity(run: Mapping[str, Any]) -> datetime:
    value = run.get("updated_at")
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        # Refused, not defaulted: a run with no last-activity time cannot be aged, and
        # guessing either way is a verdict from a measurement that did not happen.
        raise ValueError(f"run {run.get('id')!r} has no updated_at: {value!r}")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def one_page_jobs(payload: object, run_id: int, page_size: int, error: type[Exception]) -> list[dict[str, Any]]:
    """Every job of a run's latest attempt, from ONE jobs page, or `error`: a run with more
    jobs than a page holds is refused, because a waiting job on page 2 would read as no
    waiting job and a live run would be skipped as a phantom."""
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise error(f"jobs of run {run_id} was not a jobs listing: {payload!r}")
    jobs: list[dict[str, Any]] = payload["jobs"]
    if int(payload["total_count"]) > len(jobs):
        raise error(f"run {run_id} has {payload['total_count']} jobs, more than one {page_size}-job page")
    return jobs


def _has_waiting_job(run: Mapping[str, Any], jobs_of: JobsOf) -> bool:
    return any(job.get("status") in WAITING_JOB_STATUSES for job in jobs_of(int(run["id"])))


def is_phantom(run: Mapping[str, Any], now: datetime, *, jobs_of: JobsOf, after: timedelta | None = None) -> bool:
    """True when `run` is `in_progress`, idle for more than `after`, and has no waiting job."""
    if run.get("status") != PHANTOM_STATUS:
        return False
    threshold = after if after is not None else timedelta(hours=PHANTOM_AFTER_HOURS)
    if now - _last_activity(run) <= threshold:
        return False
    return not _has_waiting_job(run, jobs_of)


def split_phantoms(
    runs: Iterable[Mapping[str, Any]],
    now: datetime,
    *,
    jobs_of: JobsOf,
    after: timedelta | None = None,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """(live, phantom), each in input order."""
    live: list[Mapping[str, Any]] = []
    phantom: list[Mapping[str, Any]] = []
    for run in runs:
        (phantom if is_phantom(run, now, jobs_of=jobs_of, after=after) else live).append(run)
    return live, phantom


def phantom_line(run: Mapping[str, Any], now: datetime, *, caller: str) -> str:
    """One greppable line naming a skipped phantom by id."""
    idle_hours = (now - _last_activity(run)).total_seconds() / 3600
    return (
        f"phantom-run-skipped caller={caller} run_id={run.get('id')} "
        f"workflow={run.get('name')!r} status={run.get('status')} "
        f"updated_at={run.get('updated_at')} idle_hours={idle_hours:.1f} "
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
            f"Each was `{PHANTOM_STATUS}`, idle for more than {PHANTOM_AFTER_HOURS} h, with no job "
            "waiting for a runner (scripts/ci_phantom_runs.py). It was skipped, not waited on.\n\n"
        )
        for line in lines:
            handle.write(f"- `{line}`\n")
        handle.write("\n")


def skip_phantoms(
    runs: Sequence[T],
    probe: Callable[[T], Mapping[str, Any]],
    now: datetime,
    *,
    jobs_of: JobsOf,
    caller: str,
) -> tuple[list[T], list[T]]:
    """(live, phantom) over any run type: each phantom is printed as a warning line and
    named in the job summary, so a caller only has to iterate the live ones."""
    live: list[T] = []
    phantom: list[T] = []
    for run in runs:
        (phantom if is_phantom(probe(run), now, jobs_of=jobs_of) else live).append(run)
    lines = [phantom_line(probe(run), now, caller=caller) for run in phantom]
    for line in lines:
        print(f"::warning::{line}")
        print(line)
    write_step_summary(lines, caller=caller)
    return live, phantom
