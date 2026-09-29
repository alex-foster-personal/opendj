"""Runner-canary shard rows: GitHub jobs, classified, for either side of the pairing.

`scripts/runner_canary_report.py` reads runs from GitHub and scores vendors; this module
turns each run's jobs into `ShardRow`s. It decides what a job's outcome is (green, red,
infra, dropped, ours, pending), which vendor shards a gate-passed run owed, and counts every
owed shard GitHub never created as a lost job. Decision record:
docs/decisions/ADR-NEW-runner-canary.md.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

VENDOR_JOB = re.compile(r"canary: pytest (?P<vendor>[a-z0-9-]+) \(shard (?P<shard>\d+) of \d+\)")
BASELINE_JOB = re.compile(r"pytest fast lane \(shard (?P<shard>\d+) of \d+\)")
BASELINE_SIDE = "self-hosted"
GATE_JOB = "canary: budget gate"
# The annotation a passing budget gate leaves on its own job, naming the vendors whose shards
# the run owes (scripts/runner_canary_budget.py GateDecision.planned_vendors_notice; the two
# spellings are pinned equal by tests/scripts/test_runner_canary_rows.py).
PLANNED_VENDORS_TITLE = "runner-canary planned vendors"
#: The title the shard's own wall-budget wrapper writes on exit 124 or 137 (ci.yml and the
#: canary share the step). It is the only field that separates a TIMEOUT from a test red:
#: both fail the pytest step.
TIMEOUT_TITLE = re.compile(r"pytest fast lane TIMEOUT \(shard \d+ of \d+\)")
#: green / red: a test verdict. infra: no test at fault. dropped: no verdict (baseline
#: side). ours: a vendor-side run whose budget gate never passed, so no job reached the
#: vendor. pending: still queued or running.
Outcome = Literal["green", "red", "infra", "dropped", "ours", "pending"]


class ReadFailed(Exception):
    """A GitHub read that did not complete. UNKNOWN."""


# ----- rows -----------------------------------------------------------------------


@dataclass(frozen=True)
class ShardRow:
    vendor: str
    sha: str
    shard: int
    outcome: Outcome
    run_id: int
    run_created_at: datetime
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    never_started: bool = False
    missing: bool = False  # owed by a gate-passed run, but GitHub returned no job for it


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _required_ts(value: str) -> datetime:
    """A timestamp GitHub always sets (a run's or a job's created_at)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _never_started(job: dict[str, Any]) -> bool:
    """No runner and no steps. GitHub stamps a job cancelled in the queue with started_at
    EQUAL to created_at, not null, so started_at alone cannot say."""
    return not job.get("started_at") or (not job.get("runner_name") and not job.get("steps"))


def _timed_out(job: dict[str, Any]) -> bool:
    annotations = job.get("annotations")
    if annotations is None:
        raise ReadFailed(f"job {job.get('id')} failed but its annotations were not read")
    return any(TIMEOUT_TITLE.fullmatch(a.get("title") or "") for a in annotations)


def classify_job(job: dict[str, Any], pytest_step_name: str, timeout_minutes: int) -> Outcome:
    """green / red (pytest step failed) / infra (no test at fault) / dropped (no verdict).

    A pytest step that failed on its own wall-budget TIMEOUT is infra, not red.
    """
    if job.get("status") != "completed":
        return "dropped"
    conclusion = job.get("conclusion")
    if conclusion == "success":
        return "green"
    if conclusion == "failure":
        pytest_step = next(
            (s for s in job.get("steps") or [] if s.get("name") == pytest_step_name), None
        )
        if pytest_step is not None and pytest_step.get("conclusion") == "failure":
            return "infra" if _timed_out(job) else "red"
        return "infra"
    if conclusion == "timed_out":
        return "infra"
    if conclusion == "cancelled":
        started, completed = _ts(job.get("started_at")), _ts(job.get("completed_at"))
        ran_to_cap = (
            started is not None
            and completed is not None
            and (completed - started).total_seconds() >= timeout_minutes * 60
        )
        return "infra" if ran_to_cap else "dropped"
    return "dropped"


def _gate_job(run: dict[str, Any]) -> dict[str, Any] | None:
    gates = [j for j in run["jobs"] if j.get("name") == GATE_JOB and j.get("run_attempt", 1) == 1]
    return gates[0] if gates else None


def _gate_passed(run: dict[str, Any]) -> bool:
    gate = _gate_job(run)
    if gate is None:
        raise ReadFailed(f"canary run {run['id']} has vendor shards but no budget gate job")
    return gate.get("conclusion") == "success"


def planned_vendors(run: dict[str, Any]) -> list[str]:
    """The vendors a gate-passed run owes shards for, read from the gate's own annotation.
    Without it the owed set cannot be reconstructed, which is UNKNOWN, never zero."""
    gate = _gate_job(run)
    annotations = gate.get("annotations") if gate is not None else None
    if annotations is None:
        raise ReadFailed(
            f"canary run {run['id']}: the budget gate passed but its annotations were not "
            "read, so the vendor shards it owes are unknown"
        )
    notes = [a for a in annotations if a.get("title") == PLANNED_VENDORS_TITLE]
    if len(notes) != 1:
        raise ReadFailed(
            f"canary run {run['id']}: the budget gate passed with {len(notes)} "
            f"{PLANNED_VENDORS_TITLE!r} annotations, not 1, so the vendor shards it owes "
            "are unknown"
        )
    return [v for v in (notes[0].get("message") or "").split(",") if v]


def missing_shard_rows(
    run: dict[str, Any], observed: list[ShardRow], shard_count: int
) -> list[ShardRow]:
    """Every shard a gate-passed run owes that GitHub returned no job for. Once the run has
    finished, each is a lost job: infra, never started, so it waits forever (Sol P1 on
    b9db90801). While the run is still going, a job not yet created is pending."""
    planned = planned_vendors(run)
    stray = sorted({r.vendor for r in observed} - set(planned))
    if stray:
        raise ReadFailed(
            f"canary run {run['id']} has shards for {stray}, which its budget gate did not plan"
        )
    seen = {(r.vendor, r.shard) for r in observed}
    created = _required_ts(run["created_at"])
    outcome: Outcome = "infra" if run["status"] == "completed" else "pending"
    return [
        ShardRow(
            vendor=vendor,
            sha=run["head_sha"],
            shard=shard,
            outcome=outcome,
            run_id=run["id"],
            run_created_at=created,
            created_at=created,
            started_at=None,
            completed_at=None,
            never_started=True,
            missing=True,
        )
        for vendor in planned
        for shard in range(1, shard_count + 1)
        if (vendor, shard) not in seen
    ]


def vendor_outcome(job: dict[str, Any], gate_passed: bool, base: Outcome) -> Outcome:
    """A vendor shard's outcome. Once the gate passed, the job was the vendor's to run: a
    cancel short of the cap, a skip, or a job that never started is a lost job (infra),
    never a smaller denominator. Only a run whose gate never passed is identifiably ours."""
    if not gate_passed:
        return "ours"
    if job.get("status") != "completed":
        return "pending"
    if base == "dropped":
        return "infra"
    return base


def shard_jobs(
    runs: Iterable[dict[str, Any]],
    *,
    side: Literal["vendor", "baseline"],
    pytest_step_name: str,
    timeout_minutes: int,
    shard_count: int,
) -> list[ShardRow]:
    """Attempt-1 shard rows from runs carrying a `jobs` list, for one side of the pairing.

    On the vendor side, a gate-passed run also yields a row for every shard it owed that
    GitHub returned no job for, so a lost matrix job is counted, never a smaller denominator.
    (The baseline side needs no such rows: a baseline SHA short of a shard never pairs.)
    """
    pattern = VENDOR_JOB if side == "vendor" else BASELINE_JOB
    rows: list[ShardRow] = []
    for run in runs:
        run_rows: list[ShardRow] = []
        gate_passed: bool | None = None
        for job in run["jobs"]:
            match = pattern.fullmatch(job.get("name", ""))
            if not match or job.get("run_attempt", 1) != 1:
                continue
            outcome = classify_job(job, pytest_step_name, timeout_minutes)
            if side == "vendor":
                gate_passed = _gate_passed(run) if gate_passed is None else gate_passed
                outcome = vendor_outcome(job, gate_passed, outcome)
            run_rows.append(
                ShardRow(
                    vendor=match.group("vendor") if side == "vendor" else BASELINE_SIDE,
                    sha=run["head_sha"],
                    shard=int(match.group("shard")),
                    outcome=outcome,
                    run_id=run["id"],
                    run_created_at=_required_ts(run["created_at"]),
                    created_at=_required_ts(job["created_at"]),
                    started_at=_ts(job.get("started_at")),
                    completed_at=_ts(job.get("completed_at")),
                    never_started=_never_started(job),
                )
            )
        if side == "vendor" and _gate_job(run) is not None and _gate_passed(run):
            run_rows += missing_shard_rows(run, run_rows, shard_count)
        rows += run_rows
    return rows
