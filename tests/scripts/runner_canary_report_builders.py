"""Row builders for the runner-canary report and row tests: literal GitHub jobs-API shapes,
built the way the live API returns them, so both test modules score the same fixtures."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.runner_canary_budget import GateDecision
from scripts.runner_canary_report import evaluate_vendor
from scripts.runner_canary_rows import shard_jobs

REPO = Path(__file__).resolve().parents[2]


CONFIG = json.loads((REPO / "ci" / "runner-canary.json").read_text())


PYTEST_STEP = CONFIG["pytest_step_name"]


T0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


BARS = {
    "min_paired_commits": 20,
    "max_verdict_latency_ratio": 0.8,
    "max_p95_queue_wait_seconds": 60,
    "max_infra_failures_per_100_jobs": 1.0,
    "min_outcome_agreement": 1.0,
}


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_job(
    name: str,
    *,
    created: datetime,
    queue_s: int,
    wall_s: int,
    conclusion: str = "success",
    failed_step: str | None = None,
    attempt: int = 1,
    annotations: list[dict] | None = None,
) -> dict:
    started = created + timedelta(seconds=queue_s)
    steps = [
        {"name": "Set up job", "conclusion": "success"},
        {"name": "Provision isolated Python test environment", "conclusion": "success"},
        {"name": PYTEST_STEP, "conclusion": "success"},
    ]
    if failed_step is not None:
        for step in steps:
            if step["name"] == failed_step:
                step["conclusion"] = "failure"
                break
        else:
            raise AssertionError(failed_step)
    return {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": iso(created),
        "started_at": iso(started),
        "completed_at": iso(started + timedelta(seconds=wall_s)),
        "run_attempt": attempt,
        "runner_name": "vendor-runner-1",
        "steps": steps,
        # The report reads annotations for every failed shard job; the live shape of an
        # ordinary red carries untitled failure annotations (Tue 29 Sep 2026).
        "annotations": annotations if annotations is not None else [{"title": ""}],
    }


def never_started(job: dict, conclusion: str = "cancelled") -> dict:
    """The live shape of a job cancelled in the queue (ci.yml run, Sun 6 Sep 2026): no runner,
    no steps, and started_at stamped equal to created_at, NOT null."""
    job.update(
        conclusion=conclusion,
        runner_name="",
        steps=[],
        started_at=job["created_at"],
        completed_at=job["created_at"],
    )
    return job


TIMEOUT_ANNOTATIONS = [
    {"title": ""},
    {"title": "pytest fast lane TIMEOUT (shard 3 of 5)", "annotation_level": "failure"},
]


def workflow_run(
    sha: str, created: datetime, jobs: list[dict], run_id: int, status: str = "completed"
) -> dict:
    return {
        "id": run_id,
        "head_sha": sha,
        "created_at": iso(created),
        "status": status,
        "jobs": jobs,
    }


def planned_notice(*vendors: str) -> list[dict]:
    """The annotation a passing gate leaves, parsed from the gate's own workflow command the
    way GitHub stores it (title and message), so the two scripts cannot drift apart."""
    notice = GateDecision(vendors=list(vendors), labels={}, budgets=[]).planned_vendors_notice()
    match = re.fullmatch(r"::notice title=(?P<title>[^:]+)::(?P<message>.*)", notice)
    assert match, notice
    return [{"title": match["title"], "message": match["message"], "annotation_level": "notice"}]


def vendor_run(
    sha: str,
    created: datetime,
    *,
    vendor: str = "avrea",
    wall_s: int = 600,
    queue_s: int = 10,
    outcomes: dict[int, tuple[str, str | None]] | None = None,
    run_id: int = 1,
    gate: str = "success",
) -> dict:
    outcomes = outcomes or {}
    jobs = [
        make_job(
            "canary: budget gate",
            created=created,
            queue_s=2,
            wall_s=20,
            conclusion=gate,
            annotations=planned_notice(vendor) if gate == "success" else [],
        ),
        *[
            make_job(
                f"canary: pytest {vendor} (shard {n} of 5)",
                created=created + timedelta(seconds=30),
                queue_s=queue_s,
                wall_s=wall_s,
                conclusion=outcomes.get(n, ("success", None))[0],
                failed_step=outcomes.get(n, ("success", None))[1],
            )
            for n in range(1, 6)
        ],
    ]
    return workflow_run(sha, created, jobs, run_id)


def baseline_run(
    sha: str,
    created: datetime,
    *,
    wall_s: int = 1000,
    outcomes: dict[int, tuple[str, str | None]] | None = None,
    run_id: int = 2,
) -> dict:
    outcomes = outcomes or {}
    jobs = [
        make_job("ci scope (docs-only skip decision)", created=created, queue_s=2, wall_s=10),
        *[
            make_job(
                f"pytest fast lane (shard {n} of 5)",
                created=created + timedelta(seconds=15),
                queue_s=20,
                wall_s=wall_s,
                conclusion=outcomes.get(n, ("success", None))[0],
                failed_step=outcomes.get(n, ("success", None))[1],
            )
            for n in range(1, 6)
        ],
    ]
    return workflow_run(sha, created, jobs, run_id)


def sha_of(i: int) -> str:
    return f"{i:040x}"


RED = ("failure", PYTEST_STEP)


def paired_world(
    n_paired: int, *, vendor_wall: int = 600, baseline_wall: int = 1000, control: str | None = "red"
) -> tuple[list[dict], list[dict], list[str]]:
    """`n_paired` green SHAs on both sides, plus one known-red control SHA."""
    vendor_runs, baseline_runs = [], []
    for i in range(n_paired):
        at = T0 + timedelta(hours=i)
        vendor_runs.append(vendor_run(sha_of(i), at, wall_s=vendor_wall, run_id=1000 + i))
        baseline_runs.append(baseline_run(sha_of(i), at, wall_s=baseline_wall, run_id=2000 + i))
    known_red = []
    if control is not None:
        red_sha = sha_of(9999)
        known_red.append(red_sha)
        at = T0 + timedelta(days=5)
        baseline_runs.append(baseline_run(red_sha, at, outcomes={3: RED}, run_id=2999))
        if control == "red":
            vendor_runs.append(vendor_run(red_sha, at, outcomes={3: RED}, run_id=1999))
        elif control == "green":
            vendor_runs.append(vendor_run(red_sha, at, run_id=1999))
    return vendor_runs, baseline_runs, known_red


def evaluate(vendor_runs, baseline_runs, known_red, vendor: str = "avrea"):
    return evaluate_vendor(
        vendor,
        shard_jobs(
            vendor_runs,
            side="vendor",
            pytest_step_name=PYTEST_STEP,
            timeout_minutes=30,
            shard_count=5,
        ),
        shard_jobs(
            baseline_runs,
            side="baseline",
            pytest_step_name=PYTEST_STEP,
            timeout_minutes=60,
            shard_count=5,
        ),
        bars=BARS,
        known_red_shas=known_red,
        shard_count=5,
    )


def unpaired_vendor_run(i: int, **kwargs) -> dict:
    """A vendor run on a SHA self-hosted never finished, so it can never be paired."""
    return vendor_run(sha_of(5000 + i), T0 + timedelta(days=3, hours=i), run_id=5000 + i, **kwargs)


def keep_shards(run: dict, keep: set[int]) -> dict:
    """`run` with only the vendor shard jobs numbered in `keep`: GitHub returned no job for
    the rest, though the gate passed and planned them."""
    run["jobs"] = [
        j for j in run["jobs"] if j["name"] == "canary: budget gate" or int(j["name"][-7]) in keep
    ]
    return run
