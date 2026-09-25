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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


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


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def elapsed_seconds(started_at: str | None, completed_at: str | None) -> float | None:
    if not started_at or not completed_at:
        return None
    return max(0.0, (_parse_time(completed_at) - _parse_time(started_at)).total_seconds())


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


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "music-dj-tools-ci-cost-guard",
    }


def fetch_jobs(repository: str, run_id: str, attempt: str, token: str) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{repository}/actions/runs/{run_id}"
            f"/attempts/{attempt}/jobs?per_page=100&page={page}"
        )
        request = Request(url, headers=_headers(token))
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


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def batch_since(previous_started: str | None, now: datetime, floor: timedelta) -> str:
    """The high-water mark: the previous pass's start, never later than `now - floor`.

    The mark lives nowhere but GitHub's own record of this workflow's runs
    (`run_started_at` of the last completed pass). The floor makes two passes
    overlap even if a pass is late, and an overlap is harmless: the alert
    issue's title carries the run id and the workflow refuses to open a second.
    """
    floored = now - floor
    if previous_started is None:
        return _iso(floored)
    previous = _parse_time(previous_started)
    return _iso(min(previous, floored))


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
    mark = _parse_time(since)
    selected: list[dict[str, Any]] = []
    for run in runs:
        if run.get("name") not in watched or run.get("status") != "completed":
            continue
        if run["name"] == "E2E" and run.get("event") not in e2e_events:
            continue
        if _parse_time(run["updated_at"]) < mark:
            continue
        selected.append(run)
    return selected


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


def fetch_completed_runs(repository: str, created_since: str, token: str) -> list[dict[str, Any]]:
    """Completed runs created at or after `created_since`, newest first, all pages."""
    runs: list[dict[str, Any]] = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{repository}/actions/runs"
            f"?status=completed&per_page=100&page={page}&created=%3E%3D{created_since}"
        )
        request = Request(url, headers=_headers(token))
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
        batch = payload.get("workflow_runs") or []
        runs.extend(batch)
        if len(batch) < 100:
            return runs
        page += 1


def run_batch(args: argparse.Namespace) -> int:
    now = datetime.now(tz=UTC)
    floor = timedelta(minutes=args.overlap_minutes)
    since = batch_since(args.previous_started or None, now, floor)
    # A run completes at most `lookback` after it was created; the listing
    # filters on created_at, the selection on updated_at.
    created_since = _iso(_parse_time(since) - timedelta(hours=args.lookback_hours))
    watched = {name.strip() for name in args.watched.split(",") if name.strip()}
    e2e_events = {name.strip() for name in args.e2e_events.split(",") if name.strip()}
    runs = fetch_completed_runs(args.repository, created_since, args.token)
    priced: list[dict[str, Any]] = []
    for run in select_batch_runs(runs, watched, e2e_events, since):
        attempt = str(run.get("run_attempt") or 1)
        jobs = fetch_jobs(args.repository, str(run["id"]), attempt, args.token)
        report, result = render_markdown(
            workflow_name=run["name"], run_url=run["html_url"], run_id=str(run["id"]),
            threshold=args.threshold, jobs=jobs,
        )
        report_file = args.report_dir / f"ci-cost-report-{run['id']}.md"
        report_file.write_text(report, encoding="utf-8")
        priced.append({
            "run": run,
            "total_cost": result["total_cost"],
            "over_threshold": result["over_threshold"],
            "unknown_jobs": result["unknown_jobs"],
            "report_file": str(report_file),
        })
    summary, alerts = render_batch_summary(priced, args.threshold, since)
    print(summary)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_file:
        with Path(summary_file).open("a", encoding="utf-8") as handle:
            handle.write(summary)
    alerts_file = args.report_dir / "ci-cost-alerts.json"
    alerts_file.write_text(json.dumps(alerts, indent=2), encoding="utf-8")
    output_file = os.environ.get("GITHUB_OUTPUT", "")
    _write_output(output_file, "since", since)
    _write_output(output_file, "priced_runs", str(len(priced)))
    _write_output(output_file, "alerts", str(len(alerts)))
    _write_output(output_file, "alerts_file", str(alerts_file))
    return 0


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
    parser.add_argument("--e2e-events", default="schedule,workflow_dispatch")
    parser.add_argument("--previous-started", default="", help="run_started_at of the last pass")
    parser.add_argument("--overlap-minutes", type=int, default=30)
    parser.add_argument("--lookback-hours", type=int, default=3)
    parser.add_argument("--report-dir", type=Path, default=Path("."))
    parser.add_argument("--threshold", type=float, default=1.0)
    parser.add_argument("--token", default=os.environ.get("GH_TOKEN"))
    parser.add_argument("--jobs-json", type=Path)
    parser.add_argument("--report-file", type=Path, default=Path("ci-cost-report.md"))
    args = parser.parse_args()

    if args.batch:
        if not (args.repository and args.token and args.watched):
            parser.error("--batch needs --repository, --watched and --token (or GH_TOKEN)")
        return run_batch(args)
    if not (args.run_id and args.run_url and args.workflow_name):
        parser.error("single-run mode needs --run-id, --run-url and --workflow-name")

    if args.jobs_json:
        payload = json.loads(args.jobs_json.read_text(encoding="utf-8"))
        jobs = payload.get("jobs", payload) if isinstance(payload, dict) else payload
    else:
        if not args.repository or not args.token:
            parser.error("--repository and --token (or GH_TOKEN) are required without --jobs-json")
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
