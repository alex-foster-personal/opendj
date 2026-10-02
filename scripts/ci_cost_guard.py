#!/usr/bin/env python3
"""Estimate one GitHub Actions run's gross runner cost from its jobs.

GitHub bills hosted-runner time per job, rounding each partial minute up. This
tool intentionally reports the gross metered cost before included-minute or
other account discounts: that makes the signal stable enough to catch a
runaway even while the account still has free minutes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from scripts.ci_run_batch import (
    batch_since,
    created_floor,
    fetch_attempt,
    fetch_completed_runs,
    headers,
    iso,
    parse_time,
    reconcile_created_since,
    reconcile_listing,
)


@dataclass(frozen=True)
class RunnerSku:
    name: str
    rate_usd_per_minute: float


STANDARD_SKUS = {
    "linux-slim-x64": RunnerSku("Linux 1-core x64", 0.002),
    "linux-x64": RunnerSku("Linux 2-core x64", 0.006),
    "linux-arm64": RunnerSku("Linux 2-core arm64", 0.005),
    "windows-x64": RunnerSku("Windows 2-core x64", 0.010),
    "windows-arm64": RunnerSku("Windows 2-core arm64", 0.010),
    "macos": RunnerSku("macOS 3/4-core", 0.062),
    "self-hosted": RunnerSku("Self-hosted (GitHub charge)", 0.0),
}


def elapsed_seconds(started_at: str | None, completed_at: str | None) -> float | None:
    if not started_at or not completed_at:
        return None
    return max(0.0, (parse_time(completed_at) - parse_time(started_at)).total_seconds())


def infer_standard_sku(labels: Iterable[str]) -> RunnerSku | None:
    normalized = {label.lower() for label in labels}
    if "self-hosted" in normalized:
        return STANDARD_SKUS["self-hosted"]

    # These mappings cover the standard labels used by this repository. Fail
    # closed for custom/larger runners: silently assigning a cheap standard
    # rate would make the guard least trustworthy exactly when CI changes.
    if "ubuntu-slim" in normalized:
        return STANDARD_SKUS["linux-slim-x64"]
    if any(label.startswith("ubuntu-") and label.endswith("-arm") for label in normalized):
        return STANDARD_SKUS["linux-arm64"]
    if any(label == "ubuntu-latest" or label.startswith("ubuntu-2") for label in normalized):
        return STANDARD_SKUS["linux-x64"]
    if any(label.startswith("windows-") and label.endswith("-arm") for label in normalized):
        return STANDARD_SKUS["windows-arm64"]
    if any(label == "windows-latest" or label.startswith("windows-2") for label in normalized):
        return STANDARD_SKUS["windows-x64"]
    if any(label == "macos-latest" or label.startswith("macos-") for label in normalized):
        return STANDARD_SKUS["macos"]
    return None


def price_jobs(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    priced: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    total = 0.0

    for job in jobs:
        seconds = elapsed_seconds(job.get("started_at"), job.get("completed_at"))
        if seconds is None:
            # Skipped/unstarted jobs consume no hosted-runner time.
            continue
        # API timestamps have one-second resolution. A started hosted job that
        # appears to take zero seconds can still consume a rounded billable
        # minute, so never undercount it as free.
        minutes = max(1, math.ceil(seconds / 60))
        sku = infer_standard_sku(job.get("labels") or [])
        row = {
            "name": job.get("name", "unnamed job"),
            "url": job.get("html_url", ""),
            "conclusion": job.get("conclusion", "unknown"),
            "seconds": seconds,
            "billed_minutes": minutes,
            "labels": job.get("labels") or [],
        }
        if sku is None:
            unknown.append(row)
            continue
        cost = minutes * sku.rate_usd_per_minute
        row.update({"sku": sku.name, "rate": sku.rate_usd_per_minute, "cost": cost})
        priced.append(row)
        total += cost

    return {"jobs": priced, "unknown_jobs": unknown, "total_cost": total}


def slowest_steps(jobs: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for job in jobs:
        for step in job.get("steps") or []:
            seconds = elapsed_seconds(step.get("started_at"), step.get("completed_at"))
            if seconds is not None:
                rows.append(
                    {
                        "job": job.get("name", "unnamed job"),
                        "step": step.get("name", "unnamed step"),
                        "conclusion": step.get("conclusion", "unknown"),
                        "seconds": seconds,
                    }
                )
    return sorted(rows, key=lambda row: row["seconds"], reverse=True)[:limit]


def _escape_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(
    *,
    workflow_name: str,
    run_url: str,
    run_id: str,
    threshold: float,
    jobs: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    result = price_jobs(jobs)
    over_threshold = result["total_cost"] > threshold
    lines = [
        f"# CI cost {'alert' if over_threshold else 'report'}",
        "",
        f"- Workflow: **{_escape_cell(workflow_name)}**",
        f"- Run: [{_escape_cell(run_id)}]({run_url})",
        f"- Estimated gross hosted-runner cost: **${result['total_cost']:.3f}**",
        f"- Alert threshold: **>${threshold:.2f}**",
        "- Billing basis: each job duration rounded up to a whole minute; "
        "account discounts excluded.",
        "",
        "## Job cost breakdown",
        "",
        "| Job | Result | Runner SKU | Runtime | Billed min | Rate | Cost |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in result["jobs"]:
        name = _escape_cell(row["name"])
        job_link = f"[{name}]({row['url']})" if row["url"] else name
        lines.append(
            f"| {job_link} | {_escape_cell(row['conclusion'])} | {_escape_cell(row['sku'])} "
            f"| {row['seconds'] / 60:.2f} min | {row['billed_minutes']} "
            f"| ${row['rate']:.3f}/min | ${row['cost']:.3f} |"
        )

    if result["unknown_jobs"]:
        lines.extend(["", "## Unpriced jobs (telemetry failure)", ""])
        for row in result["unknown_jobs"]:
            labels = ", ".join(row["labels"]) or "no labels returned"
            lines.append(f"- **{_escape_cell(row['name'])}**: `{_escape_cell(labels)}`")

    steps = slowest_steps(jobs)
    if steps:
        lines.extend(
            [
                "",
                "## Slowest steps",
                "",
                "Step time is diagnostic only; runner billing is calculated per whole job.",
                "",
                "| Job | Step | Result | Duration |",
                "| --- | --- | --- | ---: |",
            ]
        )
        for row in steps:
            lines.append(
                f"| {_escape_cell(row['job'])} | {_escape_cell(row['step'])} "
                f"| {_escape_cell(row['conclusion'])} | {row['seconds'] / 60:.2f} min |"
            )

    lines.extend(
        [
            "",
            "Rates are maintained in `scripts/ci_cost_guard.py`; unknown runner labels "
            "trigger an alert instead of being guessed.",
            "",
        ]
    )
    result["over_threshold"] = over_threshold
    return "\n".join(lines), result


def fetch_jobs(repository: str, run_id: str, attempt: str, token: str) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{repository}/actions/runs/{run_id}"
            f"/attempts/{attempt}/jobs?per_page=100&page={page}"
        )
        request = Request(url, headers=headers(token, "ci-cost-guard"))
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
        batch = payload.get("jobs") or []
        jobs.extend(batch)
        if len(batch) < 100:
            return jobs
        page += 1


# ----- the batch pass: every watched completion since a high-water mark ------
#
# One scheduled job instead of one workflow_run job per completion (RUN-COUNT
# round 1, issue #2196): the Actions queue is per job, so a pricing job that
# fires on every completion holds a FIFO position ahead of PR CI whenever the
# pool is saturated. The batch prices the same runs from one job per cadence.


def select_batch_runs(
    runs: Iterable[dict[str, Any]], watched: set[str], e2e_events: set[str], since: str
) -> list[dict[str, Any]]:
    """Completed runs of watched workflows updated at or after `since`.

    Every conclusion counts, cancelled included: a run cancelled at its
    timeout bills the whole ceiling, which is the case the threshold is sized
    for. E2E is priced only on the events whose jobs can reach the alert
    (`tests/test_ci_cost_guard_workflow_coverage.py` derives that from
    e2e.yml), which is the same rule the per-completion gate used to apply.
    """
    mark = parse_time(since)
    selected: list[dict[str, Any]] = []
    for run in runs:
        if run.get("name") not in watched or run.get("status") != "completed":
            continue
        if run["name"] == "E2E" and run.get("event") not in e2e_events:
            continue
        if parse_time(run["updated_at"]) < mark:
            continue
        selected.append(run)
    return selected


def select_sink_failures(
    runs: Iterable[dict[str, Any]], sink_workflows: set[str], since: str
) -> list[dict[str, Any]]:
    """Failed runs of the sink's workflows completed at or after `since`, every event.

    The error sink rides this pass (RUN-COUNT round 3): the listing is the one the pass
    already read, so the sink costs no job of its own. Unlike pricing, E2E is not
    filtered by event: a failed pull-request E2E run is what the sink exists to record.
    """
    mark = parse_time(since)
    return [
        run
        for run in runs
        if run.get("name") in sink_workflows
        and run.get("status") == "completed"
        and run.get("conclusion") == "failure"
        and parse_time(run["updated_at"]) >= mark
    ]


def earlier_failed_attempts(
    runs: Iterable[dict[str, Any]],
    sink_workflows: set[str],
    since: str,
    read_attempt: Callable[[Any, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Failed earlier attempts of the sink's re-runs whose latest attempt completed at or
    after `since`.

    A listing shows only a run's latest attempt, so a failure that a re-run then passed
    is invisible to it; the per-completion follower posted it when it completed. In the
    48 hours to Wed 30 Sep 2026, 29 of 35 earlier attempts of re-run CI/E2E runs failed.
    One read per earlier attempt, of re-runs only; the sink's run+attempt key makes a
    re-read of an attempt already posted harmless.
    """
    mark = parse_time(since)
    failed: list[dict[str, Any]] = []
    for run in runs:
        if (
            run.get("name") not in sink_workflows
            or run.get("status") != "completed"
            or parse_time(run["updated_at"]) < mark
        ):
            continue
        for attempt in range(1, int(run.get("run_attempt") or 1)):
            earlier = read_attempt(run["id"], attempt)
            if earlier.get("conclusion") == "failure":
                failed.append(earlier)
    return failed


def reruns_no_pass_listed(
    reconciled: Iterable[dict[str, Any]], lookback: timedelta
) -> list[dict[str, Any]]:
    """The reconciled re-runs whose latest attempt completed `lookback` or more after the
    run was created: the only ones no plain pass could have listed.

    A plain pass lists every run created within `lookback` of its mark, and its mark is
    at or before every completion it covers, so a run completed inside that reach was
    listed when it completed (its earlier attempts too). Everything else a reconcile
    lists, a plain pass already posted.
    """
    return [
        run
        for run in reconciled
        if parse_time(run["updated_at"]) - parse_time(run["created_at"]) >= lookback
    ]


def sink_failure_records(
    listings: Iterable[tuple[Iterable[dict[str, Any]], str]],
    sink_workflows: set[str],
    read_attempt: Callable[[Any, int], dict[str, Any]],
    repository: str,
) -> list[dict[str, Any]]:
    """One record per failed run attempt across the pass's listings, each with its mark."""
    records: dict[tuple[str, int], dict[str, Any]] = {}
    for listing, mark in listings:
        runs = list(listing)
        for run in select_sink_failures(runs, sink_workflows, mark) + earlier_failed_attempts(
            runs, sink_workflows, mark, read_attempt
        ):
            record = sink_failure_record(run, repository)
            records[(str(record["run_id"]), record["run_attempt"])] = record
    return list(records.values())


def sink_failure_record(run: dict[str, Any], repository: str) -> dict[str, Any]:
    """What the sink step posts for one failed attempt; the attempt is part of its key."""
    attempt = int(run.get("run_attempt") or 1)
    return {
        "run_id": run["id"],
        "run_attempt": attempt,
        "workflow": run["name"],
        "conclusion": run["conclusion"],
        "head_sha": run["head_sha"],
        "url": f"https://github.com/{repository}/actions/runs/{run['id']}/attempts/{attempt}",
    }


def render_batch_summary(
    priced: list[dict[str, Any]], threshold: float, since: str
) -> tuple[str, list[dict[str, Any]]]:
    """One table for the pass, and the alerts the workflow must open."""
    lines = [
        "# CI cost guard: batch pass",
        "",
        f"- Completions priced since **{since}**: {len(priced)}",
        f"- Alert threshold: **>${threshold:.2f}** per run; unpriced runners alert too",
        "",
        "| Run | Workflow | Event | Result | Cost | Alert |",
        "| --- | --- | --- | --- | ---: | --- |",
    ]
    alerts: list[dict[str, Any]] = []
    for row in priced:
        run = row["run"]
        if row["unknown_jobs"]:
            alert = f"CI cost telemetry alert: unpriced runner in run {run['id']}"
        elif row["over_threshold"]:
            alert = f"CI cost alert: run {run['id']} estimated at ${row['total_cost']:.3f}"
        else:
            alert = ""
        if alert:
            alerts.append({"run_id": run["id"], "title": alert, "report_file": row["report_file"]})
        lines.append(
            f"| {run['id']} | {_escape_cell(run['name'])} | {run.get('event', '')} | "
            f"{run.get('conclusion', '')} | ${row['total_cost']:.3f} | {alert or 'none'} |"
        )
    return "\n".join(lines) + "\n", alerts


def run_batch(args: argparse.Namespace) -> int:
    now = datetime.now(tz=UTC)
    floor = timedelta(minutes=args.overlap_minutes)
    since = batch_since(args.previous_started or None, now, floor)
    created_since = created_floor(since, timedelta(hours=args.lookback_hours))
    watched = _names(args.watched)
    sink_workflows = _names(args.sink_workflows)
    e2e_events = {name.strip() for name in args.e2e_events.split(",") if name.strip()}
    runs = fetch_completed_runs(
        args.repository, created_since, args.token, "ci-cost-guard", workflow_names=watched
    )
    reconciled = (
        reconcile_listing(
            args.repository,
            reconcile_created_since(now, timedelta(days=args.reconcile_horizon_days)),
            created_since,
            args.token,
            workflow_names=watched,
        )
        if args.reconcile_horizon_days is not None
        else []
    )
    # Pricing is one jobs read per run, so a reconcile prices only the re-runs that
    # completed in the last few days, not every re-run it lists.
    priced_since = iso(now - timedelta(days=args.reconcile_price_days or 0))
    selected = {
        str(run["id"]): run
        for listing, mark in ((runs, since), (reconciled, priced_since))
        for run in select_batch_runs(listing, watched, e2e_events, mark)
    }

    def read_attempt(run_id: Any, attempt: int) -> dict[str, Any]:
        return fetch_attempt(args.repository, str(run_id), attempt, args.token, "ci-cost-guard")

    sink_failures = sink_failure_records(
        (
            (runs, since),
            (reruns_no_pass_listed(reconciled, timedelta(hours=args.lookback_hours)), priced_since),
        ),
        sink_workflows,
        read_attempt,
        args.repository,
    )
    priced: list[dict[str, Any]] = []
    for run in selected.values():
        attempt = str(run.get("run_attempt") or 1)
        jobs = fetch_jobs(args.repository, str(run["id"]), attempt, args.token)
        report, result = render_markdown(
            workflow_name=run["name"],
            run_url=run["html_url"],
            run_id=str(run["id"]),
            threshold=args.threshold,
            jobs=jobs,
        )
        report_file = args.report_dir / f"ci-cost-report-{run['id']}.md"
        report_file.write_text(report, encoding="utf-8")
        priced.append(
            {
                "run": run,
                "total_cost": result["total_cost"],
                "over_threshold": result["over_threshold"],
                "unknown_jobs": result["unknown_jobs"],
                "report_file": str(report_file),
            }
        )
    summary, alerts = render_batch_summary(priced, args.threshold, since)
    print(summary)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_file:
        with Path(summary_file).open("a", encoding="utf-8") as handle:
            handle.write(summary)
    alerts_file = args.report_dir / "ci-cost-alerts.json"
    alerts_file.write_text(json.dumps(alerts, indent=2), encoding="utf-8")
    sink_file = args.report_dir / "ci-sink-failures.json"
    sink_file.write_text(json.dumps(sink_failures, indent=2), encoding="utf-8")
    output_file = os.environ.get("GITHUB_OUTPUT", "")
    _write_output(output_file, "since", since)
    _write_output(output_file, "sink_failures", str(len(sink_failures)))
    _write_output(output_file, "sink_failures_file", str(sink_file))
    _write_output(output_file, "priced_runs", str(len(priced)))
    _write_output(output_file, "alerts", str(len(alerts)))
    _write_output(output_file, "alerts_file", str(alerts_file))
    return 0


def _names(csv: str) -> set[str]:
    return {name.strip() for name in csv.split(",") if name.strip()}


def _write_output(path: str, name: str, value: str) -> None:
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository")
    parser.add_argument("--run-id")
    parser.add_argument("--attempt", default="1")
    parser.add_argument("--run-url")
    parser.add_argument("--workflow-name")
    # Batch mode: one pass over every watched completion since the mark.
    parser.add_argument("--batch", action="store_true")
    parser.add_argument("--watched", default="", help="comma-separated workflow names")
    parser.add_argument(
        "--sink-workflows",
        default="",
        help="comma-separated workflows whose failures the pass posts to the error sink",
    )
    parser.add_argument("--e2e-events", default="schedule,workflow_dispatch")
    parser.add_argument("--previous-started", default="", help="run_started_at of the last pass")
    parser.add_argument("--overlap-minutes", type=int, default=30)
    parser.add_argument("--lookback-hours", type=int, default=3)
    parser.add_argument(
        "--reconcile-horizon-days",
        type=int,
        default=None,
        help="a reconcile pass: also list re-runs of runs created this many days back",
    )
    parser.add_argument(
        "--reconcile-price-days",
        type=int,
        default=None,
        help="a reconcile pass prices the re-runs that completed this many days back",
    )
    parser.add_argument("--report-dir", type=Path, default=Path("."))
    parser.add_argument("--threshold", type=float, default=1.0)
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    parser.add_argument("--jobs-json", type=Path)
    parser.add_argument("--report-file", type=Path, default=Path("ci-cost-report.md"))
    args = parser.parse_args()
    if (args.reconcile_horizon_days is None) != (args.reconcile_price_days is None):
        parser.error("--reconcile-horizon-days and --reconcile-price-days go together")

    if args.batch:
        if not (args.repository and args.token and args.watched):
            parser.error("--batch needs --repository, --watched and --token (or GITHUB_TOKEN)")
        unlisted = sorted(_names(args.sink_workflows) - _names(args.watched))
        if not args.sink_workflows or unlisted:
            parser.error(
                f"--sink-workflows must name workflows in --watched; not listed: {unlisted}"
            )
        return run_batch(args)
    if not (args.run_id and args.run_url and args.workflow_name):
        parser.error("single-run mode needs --run-id, --run-url and --workflow-name")

    if args.jobs_json:
        payload = json.loads(args.jobs_json.read_text(encoding="utf-8"))
        jobs = payload.get("jobs", payload) if isinstance(payload, dict) else payload
    else:
        if not args.repository or not args.token:
            parser.error("--repository and --token (or GITHUB_TOKEN) are required without --jobs-json")
        jobs = fetch_jobs(args.repository, args.run_id, args.attempt, args.token)

    report, result = render_markdown(
        workflow_name=args.workflow_name,
        run_url=args.run_url,
        run_id=args.run_id,
        threshold=args.threshold,
        jobs=jobs,
    )
    args.report_file.write_text(report, encoding="utf-8")
    print(report)

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_file:
        with Path(summary_file).open("a", encoding="utf-8") as handle:
            handle.write(report)

    output_file = os.environ.get("GITHUB_OUTPUT", "")
    _write_output(output_file, "estimated_cost_usd", f"{result['total_cost']:.3f}")
    _write_output(output_file, "over_threshold", str(result["over_threshold"]).lower())
    _write_output(output_file, "unpriced_jobs", str(bool(result["unknown_jobs"])).lower())
    _write_output(output_file, "report_file", str(args.report_file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
