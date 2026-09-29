"""Read-only collection and reporting for a CI evaluation campaign.

This leaf owns GitHub Actions run correlation and Markdown report rendering.
It deliberately accepts the caller's command runner so collection remains
read-only and testable without a live GitHub API.
"""

from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime
from typing import Any

from scripts.ci_cost_guard import price_jobs

CASE_COUNT = 10


class CampaignError(RuntimeError):
    """Raised when a campaign cannot proceed safely."""


def _json_command(runner: Any, args: list[str]) -> Any:
    result = runner.run(args)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CampaignError(f"command did not return JSON: {shlex.join(args)}") from exc


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def collect_campaign(campaign: dict[str, Any], runner: Any) -> dict[str, Any]:  # noqa: C901
    """Collect workflow and cost evidence for every submitted campaign PR."""
    prs = campaign.get("prs") or []
    if len(prs) != CASE_COUNT:
        raise CampaignError("collect requires all ten PRs to have been submitted")
    if any(not row.get("number") or not row.get("url") for row in prs):
        raise CampaignError("collect requires all ten PRs to have been submitted")
    if not campaign.get("prepared_at"):
        raise CampaignError("campaign is missing its prepared_at timestamp")
    target = campaign["target_repository"]
    cases: list[dict[str, Any]] = []
    anomalies: list[str] = []
    prepared_at = _timestamp(campaign["prepared_at"])
    all_runs = _json_command(
        runner,
        [
            "gh", "run", "list", "--repo", target, "--limit", "100", "--json",
            "databaseId,workflowName,status,conclusion,url,createdAt,updatedAt,headBranch,headSha,event",
        ],
    )
    known_workflows = {*campaign["expected_workflows"], "CI Cost Guard"}
    campaign_runs = [
        row for row in all_runs
        if row.get("workflowName") in known_workflows
        and row.get("createdAt")
        and _timestamp(row["createdAt"]) >= prepared_at
    ]
    priced_runs: dict[int, dict[str, Any]] = {}
    total_cost = 0.0
    for run in campaign_runs:
        run_id = run.get("databaseId")
        priced = {"jobs": [], "unknown_jobs": [], "total_cost": 0.0}
        if run.get("status") == "completed" and run_id is not None:
            payload = _json_command(
                runner, ["gh", "api", f"repos/{target}/actions/runs/{run_id}/jobs?per_page=100"]
            )
            priced = price_jobs(payload.get("jobs") or [])
            total_cost += priced["total_cost"]
        if run_id is not None:
            priced_runs[int(run_id)] = priced
    for pr in campaign["prs"]:
        matching = [
            row for row in campaign_runs
            if row.get("event") == "pull_request" and row.get("headBranch") == pr["branch"]
        ]
        workflow_rows = []
        for expected in campaign["expected_workflows"]:
            selected = [row for row in matching if row.get("workflowName") == expected]
            if not selected:
                anomalies.append(f"PR #{pr['number']}: missing workflow {expected}")
                workflow_rows.append({"workflow": expected, "status": "missing", "cost_usd": 0.0})
                continue
            if len(selected) > 1:
                anomalies.append(f"PR #{pr['number']}: duplicate workflow {expected} runs ({len(selected)})")
            run = max(selected, key=lambda row: row.get("createdAt") or "")
            priced = priced_runs.get(int(run["databaseId"]), {"jobs": [], "unknown_jobs": [], "total_cost": 0.0})
            if run.get("status") == "completed":
                if priced["total_cost"] > 1.0:
                    anomalies.append(f"PR #{pr['number']}: {expected} estimated at ${priced['total_cost']:.3f}")
                if priced["unknown_jobs"]:
                    anomalies.append(f"PR #{pr['number']}: {expected} has unpriced runner jobs")
            workflow_rows.append({
                "workflow": expected, "run_id": run.get("databaseId"), "url": run.get("url"),
                "status": run.get("status"), "conclusion": run.get("conclusion"),
                "cost_usd": priced["total_cost"], "jobs": priced["jobs"],
                "unknown_jobs": priced["unknown_jobs"],
            })
        cases.append({**pr, "workflows": workflow_rows})
    direct_complete = all(
        workflow["status"] == "completed" for case in cases for workflow in case["workflows"]
    )
    monitored_completed = [
        row for row in campaign_runs
        if row.get("workflowName") in campaign["expected_workflows"] and row.get("status") == "completed"
    ]
    # The cost guard is a scheduled batch pass (Tue 22 Sep 2026, #2196): one
    # pass prices every watched completion since the previous pass, so coverage
    # is complete once a guard pass has COMPLETED that STARTED after the last
    # monitored completion, not once there is one guard run per monitored run.
    guard_runs = [row for row in campaign_runs if row.get("workflowName") == "CI Cost Guard"]
    guard_completed = [row for row in guard_runs if row.get("status") == "completed"]
    last_monitored = max((row.get("updatedAt") or "" for row in monitored_completed), default="")
    guard_coverage_complete = any(
        (row.get("createdAt") or "") > last_monitored for row in guard_completed
    )
    if direct_complete and not guard_coverage_complete:
        anomalies.append(
            "Cost Guard coverage incomplete: no completed guard pass started after the "
            f"last monitored completion at {last_monitored or 'unknown'}"
        )
    complete = direct_complete and guard_coverage_complete
    run_rows = []
    for run in sorted(campaign_runs, key=lambda row: row.get("createdAt") or ""):
        priced = priced_runs.get(int(run["databaseId"]), {"jobs": [], "unknown_jobs": [], "total_cost": 0.0})
        run_rows.append({**run, "cost_usd": priced["total_cost"], "jobs": priced["jobs"], "unknown_jobs": priced["unknown_jobs"]})
    return {
        "schema_version": 1, "run_id": campaign["run_id"],
        "source_repository": campaign["source_repository"], "source_ref": campaign["source_ref"],
        "target_repository": target, "collected_at": datetime.now(UTC).isoformat(),
        "complete": complete, "estimated_gross_cost_usd": total_cost,
        "monitored_runs_completed": len(monitored_completed),
        "cost_guard_runs_completed": len(guard_completed), "anomalies": anomalies,
        "cases": cases, "campaign_runs": run_rows,
    }


def render_campaign_report(result: dict[str, Any]) -> str:
    """Render the collection snapshot and the required human assessment prompt."""
    lines = [
        "# CI evaluation campaign snapshot", "", f"- Run: `{result['run_id']}`",
        f"- Source: `{result['source_repository']}@{result['source_ref']}`",
        f"- Disposable target: `{result['target_repository']}`",
        f"- Complete: **{str(result['complete']).lower()}**",
        f"- Estimated gross campaign cost, including baseline and guard runs: **${result['estimated_gross_cost_usd']:.3f}**",
        f"- Completed monitored runs: **{result.get('monitored_runs_completed', 0)}**",
        f"- Completed Cost Guard runs: **{result.get('cost_guard_runs_completed', 0)}**", "",
        "| PR | Workflow | Status | Result | Estimated cost |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for case in result["cases"]:
        for workflow in case["workflows"]:
            pr = f"[#{case['number']}]({case['url']})"
            run_name = workflow["workflow"]
            if workflow.get("url"):
                run_name = f"[{run_name}]({workflow['url']})"
            lines.append(
                f"| {pr} | {run_name} | {workflow['status']} | "
                f"{workflow.get('conclusion') or '-'} | ${workflow['cost_usd']:.3f} |"
            )
    lines.extend(["", "## Mechanical anomalies", ""])
    if result["anomalies"]:
        lines.extend(f"- {item}" for item in result["anomalies"])
    else:
        lines.append("- None in this snapshot.")
    lines.extend([
        "", "## Required LLM assessment", "",
        "This snapshot is evidence, not the verdict. Use `$af-evalsuite-ci` to inspect "
        "failed logs, compare all ten PRs, review Cost Guard runs/issues, and write the "
        "final assessment.", "",
    ])
    return "\n".join(lines)
