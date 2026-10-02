import json
import subprocess
from pathlib import Path

import pytest

from scripts.ci_eval_collection import collect_campaign, render_campaign_report
from scripts.ci_eval_suite import (
    CASE_COUNT,
    CampaignError,
    CommandResult,
    CommandRunner,
    build_campaign,
    main,
    validate_target,
    write_json,
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


def test_collect_matches_unique_pr_branches_when_monitored_runs_complete(tmp_path):
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
    assert result["estimated_gross_cost_usd"] == pytest.approx(20 * 0.006)
    assert result["anomalies"] == []


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _origin_ref_sha(origin: Path, branch: str) -> str:
    return _git(origin, "rev-parse", f"refs/heads/{branch}")


def _prepared_submit_fixture(tmp_path: Path) -> tuple[dict, Path, Path]:
    campaign = _campaign(tmp_path)
    checkout = Path(campaign["checkout"])
    origin = tmp_path / "origin.git"
    manifest = tmp_path / "campaign.json"

    _git(tmp_path, "init", "--bare", str(origin))
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-b", campaign["base_branch"])
    _git(checkout, "config", "user.name", "AF CI Eval Suite Test")
    _git(checkout, "config", "user.email", "ci-eval-test@example.com")
    _git(checkout, "config", "commit.gpgsign", "false")
    (checkout / "README").write_text("base\n", encoding="utf-8")
    _git(checkout, "add", "README")
    _git(checkout, "commit", "-m", "base")
    _git(checkout, "remote", "add", "origin", str(origin))
    _git(checkout, "push", "-u", "origin", "HEAD")

    campaign["state"] = "prepared"
    write_json(manifest, campaign)
    return campaign, manifest, origin


class _SubmitGhStub(CommandRunner):
    """Real git; stub only gh. First ``pr create`` fails to interrupt after push."""

    def __init__(self, target: str) -> None:
        self.target = target
        self.git_pushes: list[list[str]] = []
        self._create_calls = 0
        self._next_pr = 1

    def run(self, args, *, cwd=None, check=True):
        argv = list(args)
        if argv[0] == "git":
            if len(argv) > 1 and argv[1] == "push":
                self.git_pushes.append(argv)
            return super().run(argv, cwd=cwd, check=check)
        if argv[0] != "gh":
            raise AssertionError(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return CommandResult(1, "", "no pull requests found")
        if argv[:3] == ["gh", "pr", "create"]:
            self._create_calls += 1
            if self._create_calls == 1:
                result = CommandResult(1, "", "HTTP 502")
                if check:
                    detail = result.stderr.strip() or result.stdout.strip()
                    raise CampaignError(f"command failed ({result.returncode}): {detail}")
                return result
            number = self._next_pr
            self._next_pr += 1
            return CommandResult(0, f"https://github.test/{self.target}/pull/{number}\n", "")
        raise AssertionError(argv)


def test_submit_resume_reuses_already_pushed_remote_branch(tmp_path, monkeypatch):
    campaign, manifest, origin = _prepared_submit_fixture(tmp_path)
    target = campaign["target_repository"]
    case_one_branch = campaign["cases"][0]["branch"]
    runner = _SubmitGhStub(target)
    submit_args = [
        "submit",
        "--manifest",
        str(manifest),
        "--execute",
        "--confirm-target",
        target,
    ]

    with pytest.raises(SystemExit) as interrupted:
        main(submit_args, runner=runner)
    assert interrupted.value.code == 2

    sha_after_interrupt = _origin_ref_sha(origin, case_one_branch)
    assert sha_after_interrupt

    crashed = json.loads(manifest.read_text(encoding="utf-8"))
    crashed["prs"] = []
    crashed.pop("pushed", None)
    write_json(manifest, crashed)
    # Recreate-from-base would mint a sibling commit; pin dates so that SHA
    # cannot collide with the already-pushed object and mask non-fast-forward.
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2020-01-02T00:00:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2020-01-02T00:00:00")

    assert main(["submit", "--resume", *submit_args[1:]], runner=runner) == 0

    assert _origin_ref_sha(origin, case_one_branch) == sha_after_interrupt
    for argv in runner.git_pushes:
        assert "--force" not in argv
        assert "--force-with-lease" not in argv
        assert not any(token.startswith("+") for token in argv[2:])

    submitted = json.loads(manifest.read_text(encoding="utf-8"))
    assert submitted["state"] == "submitted"
    assert len(submitted["prs"]) == CASE_COUNT
    assert all(row.get("number") and row.get("url") for row in submitted["prs"])
    assert submitted["prs"][0]["head_sha"] == sha_after_interrupt
    for case in campaign["cases"]:
        assert _origin_ref_sha(origin, case["branch"])
