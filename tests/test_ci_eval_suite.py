import json
from pathlib import Path

import pytest

from scripts.ci_eval_collection import collect_campaign, render_campaign_report
from scripts.ci_eval_suite import (
    CASE_COUNT,
    CampaignError,
    CommandResult,
    build_campaign,
    validate_target,
)


def _campaign(tmp_path: Path):
    return build_campaign(
        source_repository="owner/music-dj-tools",
        source_ref="af--rekordbox-parity-ui",
        target_repository="owner/music-dj-tools-ci-eval-20260816",
        run_id="20260816t220000z",
        workspace=tmp_path,
    )


def test_plan_has_exactly_ten_unique_harmless_pr_cases(tmp_path):
    campaign = _campaign(tmp_path)

    assert len(campaign["cases"]) == CASE_COUNT == 10
    assert len({case["branch"] for case in campaign["cases"]}) == 10
    assert len({case["marker"] for case in campaign["cases"]}) == 10
    assert all(case["marker"].startswith(".ci-eval/cases/") for case in campaign["cases"])
    assert campaign["expected_workflows"] == ["CI", "Build docs"]


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("owner/repo", "owner/repo"),
        ("owner/repo", "owner/production-copy"),
        ("bad", "owner/repo-ci-eval"),
    ],
)
def test_target_guard_rejects_production_or_ambiguous_repositories(source, target):
    with pytest.raises(CampaignError):
        validate_target(source, target)


def test_manifest_is_json_serializable_and_keeps_workspace_absolute(tmp_path):
    campaign = _campaign(tmp_path)

    restored = json.loads(json.dumps(campaign))

    assert restored["workspace"] == str(tmp_path.resolve())
    assert restored["checkout"] == str((tmp_path / "checkout").resolve())


def test_report_requires_llm_assessment_and_surfaces_anomalies():
    result = {
        "run_id": "run-one",
        "source_repository": "owner/source",
        "source_ref": "main",
        "target_repository": "owner/source-ci-eval-run-one",
        "complete": True,
        "estimated_gross_cost_usd": 1.234,
        "anomalies": ["PR #2: missing workflow CI"],
        "cases": [
            {
                "number": 2,
                "url": "https://github.test/pull/2",
                "workflows": [
                    {
                        "workflow": "CI",
                        "url": None,
                        "status": "missing",
                        "conclusion": None,
                        "cost_usd": 0.0,
                    }
                ],
            }
        ],
    }

    report = render_campaign_report(result)

    assert "**$1.234**" in report
    assert "PR #2: missing workflow CI" in report
    assert "Required LLM assessment" in report
    assert "$af-evalsuite-ci" in report


def test_collect_matches_unique_pr_branches_and_waits_for_cost_guard_coverage(tmp_path):
    campaign = _campaign(tmp_path)
    campaign["prepared_at"] = "2026-08-16T22:00:00+00:00"
    campaign["prs"] = [
        {
            "case_id": case["id"],
            "branch": case["branch"],
            "head_sha": f"sha-{case['id']}",
            "number": number,
            "url": f"https://github.test/pull/{number}",
        }
        for number, case in enumerate(campaign["cases"], 1)
    ]
    runs = []
    run_id = 100
    for pr in campaign["prs"]:
        for workflow in campaign["expected_workflows"]:
            runs.append(
                {
                    "databaseId": run_id,
                    "workflowName": workflow,
                    "status": "completed",
                    "conclusion": "success",
                    "url": f"https://github.test/runs/{run_id}",
                    "createdAt": "2026-08-16T22:01:00Z",
                    "updatedAt": "2026-08-16T22:02:00Z",
                    "headBranch": pr["branch"],
                    # Pull request workflows may expose a synthesized merge SHA.
                    "headSha": f"merge-{pr['head_sha']}",
                    "event": "pull_request",
                }
            )
            run_id += 1
    for _ in range(20):
        runs.append(
            {
                "databaseId": run_id,
                "workflowName": "CI Cost Guard",
                "status": "completed",
                "conclusion": "success",
                "url": f"https://github.test/runs/{run_id}",
                "createdAt": "2026-08-16T22:03:00Z",
                "updatedAt": "2026-08-16T22:04:00Z",
                "headBranch": campaign["base_branch"],
                "headSha": "base-sha",
                "event": "workflow_run",
            }
        )
        run_id += 1

    class FakeRunner:
        def run(self, args, *, cwd=None, check=True):
            if args[:3] == ["gh", "run", "list"]:
                return CommandResult(0, json.dumps(runs), "")
            if args[:2] == ["gh", "api"]:
                jobs = {
                    "jobs": [
                        {
                            "name": "bounded",
                            "labels": ["ubuntu-latest"],
                            "started_at": "2026-08-16T22:00:00Z",
                            "completed_at": "2026-08-16T22:00:30Z",
                            "conclusion": "success",
                            "html_url": "https://github.test/jobs/1",
                        }
                    ]
                }
                return CommandResult(0, json.dumps(jobs), "")
            raise AssertionError(args)

    result = collect_campaign(campaign, FakeRunner())

    assert result["complete"] is True
    assert result["monitored_runs_completed"] == 20
    assert result["cost_guard_runs_completed"] == 20
    assert result["estimated_gross_cost_usd"] == pytest.approx(40 * 0.006)
    assert result["anomalies"] == []
