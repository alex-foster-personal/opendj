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
from datetime import datetime
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


def fetch_jobs(repository: str, run_id: str, attempt: str, token: str) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{repository}/actions/runs/{run_id}"
            f"/attempts/{attempt}/jobs?per_page=100&page={page}"
        )
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "music-dj-tools-ci-cost-guard",
            },
        )
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
        batch = payload.get("jobs") or []
        jobs.extend(batch)
        if len(batch) < 100:
            return jobs
        page += 1


def _write_output(path: str, name: str, value: str) -> None:
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", default="1")
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--workflow-name", required=True)
    parser.add_argument("--threshold", type=float, default=1.0)
    parser.add_argument("--token", default=os.environ.get("GH_TOKEN"))
    parser.add_argument("--jobs-json", type=Path)
    parser.add_argument("--report-file", type=Path, default=Path("ci-cost-report.md"))
    args = parser.parse_args()

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
