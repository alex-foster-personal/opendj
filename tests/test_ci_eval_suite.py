import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.ci_eval_collection import collect_campaign, render_campaign_report
from scripts.ci_eval_suite import (
    CAMPAIGN_WINDOW_SKEW_MARGIN,
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
    # The baseline push's own runs: without them coverage is incomplete.
    for baseline_id, workflow in ((1, "CI"), (2, "Build docs")):
        runs.append(
            {
                "databaseId": baseline_id,
                "workflowName": workflow,
                "status": "completed",
                "conclusion": "success",
                "url": f"https://github.test/runs/{baseline_id}",
                "createdAt": "2026-08-16T22:00:30Z",
                "updatedAt": "2026-08-16T22:01:30Z",
                "headBranch": campaign["base_branch"],
                "headSha": "baseline-sha",
                "event": "push",
            }
        )

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
    assert result["monitored_runs_completed"] == 22
    assert result["estimated_gross_cost_usd"] == pytest.approx(22 * 0.006)
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


class _FirstPushFailsStub(_SubmitGhStub):
    """Real git except the first ``git push``, which fails before reaching origin."""

    def __init__(self, target: str) -> None:
        """Start with the first push armed to fail and every PR create succeeding."""
        super().__init__(target)
        self._create_calls = 1
        self._push_failed = False

    def run(self, args, *, cwd=None, check=True):
        """Interrupt the very first push; delegate everything else to the gh stub."""
        argv = list(args)
        if argv[:2] == ["git", "push"] and not self._push_failed:
            self._push_failed = True
            raise CampaignError("command failed (128): connection reset")
        return super().run(argv, cwd=cwd, check=check)


def _submit_argv(manifest: Path, target: str, *extra: str) -> list[str]:
    """Build the live submit argv for a manifest and confirmed target."""
    return ["submit", *extra, "--manifest", str(manifest), "--execute", "--confirm-target", target]


def test_submit_resume_after_interrupt_before_first_push_creates_branch(tmp_path):
    """Resume after a failed first push creates the branch once and finishes all cases."""
    campaign, manifest, origin = _prepared_submit_fixture(tmp_path)
    target = campaign["target_repository"]
    runner = _FirstPushFailsStub(target)

    with pytest.raises(SystemExit):
        main(_submit_argv(manifest, target), runner=runner)
    with pytest.raises(subprocess.CalledProcessError):
        _origin_ref_sha(origin, campaign["cases"][0]["branch"])

    assert main(_submit_argv(manifest, target, "--resume"), runner=runner) == 0

    submitted = json.loads(manifest.read_text(encoding="utf-8"))
    assert submitted["state"] == "submitted"
    assert [row["case_id"] for row in submitted["prs"]] == [case["id"] for case in campaign["cases"]]
    assert len({row["number"] for row in submitted["prs"]}) == CASE_COUNT
    for case, row in zip(campaign["cases"], submitted["prs"], strict=True):
        assert _origin_ref_sha(origin, case["branch"]) == row["head_sha"]


def test_submit_resume_refuses_conflicting_remote_branch(tmp_path, capsys):
    """A remote case branch that is not this campaign's case is reported, never reused or overwritten."""
    campaign, manifest, origin = _prepared_submit_fixture(tmp_path)
    target = campaign["target_repository"]
    branch = campaign["cases"][0]["branch"]
    checkout = Path(campaign["checkout"])
    _git(checkout, "switch", "-c", branch)
    (checkout / "foreign.txt").write_text("someone else\n", encoding="utf-8")
    _git(checkout, "add", "foreign.txt")
    _git(checkout, "commit", "-m", "foreign")
    _git(checkout, "push", "origin", branch)
    _git(checkout, "switch", campaign["base_branch"])
    foreign_sha = _origin_ref_sha(origin, branch)
    runner = _SubmitGhStub(target)

    with pytest.raises(SystemExit) as refused:
        main(_submit_argv(manifest, target, "--resume"), runner=runner)

    assert refused.value.code == 2
    assert "remote branch conflict" in capsys.readouterr().err
    assert _origin_ref_sha(origin, branch) == foreign_sha
    assert runner.git_pushes == []
    assert runner._create_calls == 0


class _PrepareGhStub(CommandRunner):
    """Real git; stub only gh. Records whether prepared_at is on disk at push."""

    def __init__(
        self,
        *,
        origin: Path,
        manifest: Path,
        base_branch: str,
    ) -> None:
        self.origin = origin
        self.manifest = manifest
        self.base_branch = base_branch
        self.prepared_at_when_pushing: str | None = None
        self.push_argv: list[str] | None = None

    def run(self, args, *, cwd=None, check=True):
        argv = list(args)
        if argv[0] == "git":
            if len(argv) > 1 and argv[1] == "push":
                self.push_argv = argv
                if self.manifest.exists():
                    payload = json.loads(self.manifest.read_text(encoding="utf-8"))
                    self.prepared_at_when_pushing = payload.get("prepared_at")
            return super().run(argv, cwd=cwd, check=check)
        if argv[0] != "gh":
            raise AssertionError(argv)
        return self._run_gh(argv, check=check)

    def _run_gh(self, argv: list[str], *, check: bool) -> CommandResult:
        if argv[:3] == ["gh", "auth", "status"]:
            return CommandResult(0, "", "")
        if argv[:3] == ["gh", "repo", "view"]:
            return self._repo_view(argv, check=check)
        if argv[:3] == ["gh", "repo", "create"]:
            return CommandResult(0, "", "")
        if argv[:3] == ["gh", "repo", "clone"]:
            self._materialize_clone(Path(argv[4]))
            return CommandResult(0, "", "")
        if argv[:3] == ["gh", "repo", "edit"]:
            return CommandResult(0, "", "")
        raise AssertionError(argv)

    def _repo_view(self, argv: list[str], *, check: bool) -> CommandResult:
        if "description,isPrivate" in argv:
            result = CommandResult(1, "", "not found")
            if check:
                raise CampaignError(f"command failed ({result.returncode}): {result.stderr}")
            return result
        if ".sshUrl" in argv or "sshUrl" in argv:
            return CommandResult(0, f"{self.origin}\n", "")
        raise AssertionError(argv)

    def _materialize_clone(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        _git(dest, "init", "-b", self.base_branch)
        _git(dest, "config", "user.name", "AF CI Eval Suite Test")
        _git(dest, "config", "user.email", "ci-eval-test@example.com")
        _git(dest, "config", "commit.gpgsign", "false")
        (dest / "README").write_text("base\n", encoding="utf-8")
        _git(dest, "add", "README")
        _git(dest, "commit", "-m", "base")
        _git(dest, "remote", "add", "origin", str(dest.parent / "source.git"))


def test_prepare_records_campaign_window_before_baseline_push(tmp_path):
    before_prepare = datetime.now(UTC)
    prepared, runner, origin = _prepare_real_campaign(tmp_path)

    assert runner.push_argv is not None
    assert f"HEAD:refs/heads/{prepared['base_branch']}" in runner.push_argv
    assert runner.prepared_at_when_pushing, "prepared_at must already be persisted when the baseline push runs"
    assert prepared["state"] == "prepared"
    assert prepared["prepared_at"] == runner.prepared_at_when_pushing
    # The window opens a bounded skew margin early, never arbitrarily early:
    # an unbounded back-off would sweep unrelated older runs into the campaign.
    window = datetime.fromisoformat(prepared["prepared_at"])
    assert window >= before_prepare - CAMPAIGN_WINDOW_SKEW_MARGIN - timedelta(seconds=1)
    assert window <= before_prepare
    assert _origin_ref_sha(origin, prepared["base_branch"])


def test_prepare_resume_keeps_existing_prepared_at(tmp_path):
    campaign, manifest, _origin = _prepared_submit_fixture(tmp_path)
    original = "2026-08-16T21:59:00+00:00"
    campaign["prepared_at"] = original
    write_json(manifest, campaign)
    description = f"Disposable CI evaluation mirror [af-evalsuite-ci:{campaign['run_id']}]"

    class _ResumeGhStub(CommandRunner):
        def run(self, args, *, cwd=None, check=True):
            argv = list(args)
            if argv[0] == "git":
                raise AssertionError(argv)
            if argv[:3] == ["gh", "auth", "status"]:
                return CommandResult(0, "", "")
            if argv[:3] == ["gh", "repo", "view"]:
                return CommandResult(
                    0,
                    json.dumps({"description": description, "isPrivate": True}),
                    "",
                )
            raise AssertionError(argv)

    assert (
        main(
            [
                "prepare",
                "--resume",
                "--manifest",
                str(manifest),
                "--execute",
                "--confirm-target",
                campaign["target_repository"],
            ],
            runner=_ResumeGhStub(),
        )
        == 0
    )

    restored = json.loads(manifest.read_text(encoding="utf-8"))
    assert restored["prepared_at"] == original
    assert restored["state"] == "prepared"


def _run_row(*, run_id: int, workflow: str, created_at: str, branch: str, event: str) -> dict:
    return {
        "databaseId": run_id,
        "workflowName": workflow,
        "status": "completed",
        "conclusion": "success",
        "url": f"https://github.test/runs/{run_id}",
        "createdAt": created_at,
        "updatedAt": created_at,
        "headBranch": branch,
        "headSha": f"sha-{run_id}",
        "event": event,
    }


class _CollectListRunner:
    def __init__(self, runs: list[dict]) -> None:
        self.runs = runs

    def run(self, args, *, cwd=None, check=True):
        if args[:3] == ["gh", "run", "list"]:
            return CommandResult(0, json.dumps(self.runs), "")
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


def _gh_created_at(moment: datetime) -> str:
    """Render a moment the way `gh run list` reports createdAt: whole seconds, Z."""
    return moment.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _prepare_real_campaign(tmp_path: Path) -> tuple[dict, _PrepareGhStub, Path]:
    """Run the real prepare path: real git and bare remote, only gh stubbed."""
    campaign = _campaign(tmp_path)
    manifest = tmp_path / "campaign.json"
    origin = tmp_path / "origin.git"
    write_json(manifest, campaign)
    _git(tmp_path, "init", "--bare", str(origin))
    runner = _PrepareGhStub(origin=origin, manifest=manifest, base_branch=campaign["base_branch"])
    argv = [
        "prepare",
        "--manifest",
        str(manifest),
        "--execute",
        "--confirm-target",
        campaign["target_repository"],
    ]
    assert main(argv, runner=runner) == 0
    return json.loads(manifest.read_text(encoding="utf-8")), runner, origin


def _submitted_rows(campaign: dict) -> list[dict]:
    return [
        {
            "case_id": case["id"],
            "branch": case["branch"],
            "head_sha": f"sha-{case['id']}",
            "number": number,
            "url": f"https://github.test/pull/{number}",
        }
        for number, case in enumerate(campaign["cases"], 1)
    ]


def _campaign_runs(campaign: dict, baseline_created: datetime) -> list[dict]:
    """Baseline push runs stamped in the push's own second, then every PR run.

    The baseline CI finishes LAST (updatedAt ten minutes after the push), well
    after every PR workflow has completed.
    """
    base = campaign["base_branch"]
    runs = []
    for run_id, workflow in ((1, "CI"), (2, "Build docs")):
        row = _run_row(
            run_id=run_id,
            workflow=workflow,
            created_at=_gh_created_at(baseline_created),
            branch=base,
            event="push",
        )
        row["updatedAt"] = _gh_created_at(baseline_created + timedelta(minutes=10))
        runs.append(row)
    run_id = 100
    for pr in campaign["prs"]:
        for workflow in campaign["expected_workflows"]:
            runs.append(
                _run_row(
                    run_id=run_id,
                    workflow=workflow,
                    created_at=_gh_created_at(baseline_created + timedelta(minutes=1)),
                    branch=pr["branch"],
                    event="pull_request",
                )
            )
            run_id += 1
    return runs


def _prepared_campaign_with_runs(tmp_path: Path, *, github_clock_behind_s: int = 0):
    before_prepare = datetime.now(UTC)
    campaign, _runner, _origin = _prepare_real_campaign(tmp_path)
    campaign["prs"] = _submitted_rows(campaign)
    baseline_created = before_prepare - timedelta(seconds=github_clock_behind_s)
    return campaign, _campaign_runs(campaign, baseline_created), baseline_created


@pytest.mark.parametrize("github_clock_behind_s", [0, 5])
def test_prepared_window_includes_baseline_runs_created_in_the_push_second(tmp_path, github_clock_behind_s):
    # GitHub's createdAt is whole seconds on GitHub's clock. The second BEFORE
    # prepare starts is the earliest stamp the baseline push's own runs can
    # carry with synchronized clocks, and a few seconds earlier still when
    # GitHub's clock trails this host's. The window must be open at both.
    campaign, runs, _baseline_created = _prepared_campaign_with_runs(
        tmp_path, github_clock_behind_s=github_clock_behind_s
    )

    result = collect_campaign(campaign, _CollectListRunner(runs))

    baseline_ids = {row["databaseId"] for row in result["campaign_runs"] if row.get("event") == "push"}
    assert baseline_ids == {1, 2}, "the baseline push's own CI / docs runs fell outside the window"
    assert result["monitored_runs_completed"] == 2 + 2 * CASE_COUNT
    # Control: admitting the baseline must not make coverage impossible. With
    # every in-window run finished, the campaign is complete and fully priced.
    assert result["complete"] is True
    assert result["anomalies"] == []
    assert result["estimated_gross_cost_usd"] > 0


@pytest.mark.parametrize("baseline_status", ["queued", "in_progress"])
def test_unfinished_baseline_run_keeps_coverage_incomplete(tmp_path, baseline_status):
    # Every PR workflow has completed, but the baseline push's CI is still
    # running, so its cost is unpriced. The snapshot must not claim complete
    # coverage (Codex P2 on PR #3943).
    campaign, runs, _baseline_created = _prepared_campaign_with_runs(tmp_path)
    baseline_ci = next(row for row in runs if row["databaseId"] == 1)
    baseline_ci["status"] = baseline_status
    baseline_ci["conclusion"] = None

    result = collect_campaign(campaign, _CollectListRunner(runs))

    assert all(workflow["status"] == "completed" for case in result["cases"] for workflow in case["workflows"]), (
        "fixture must have every PR workflow finished, or this test proves nothing"
    )
    assert result["complete"] is False
    incomplete = [item for item in result["anomalies"] if item.startswith("Coverage incomplete")]
    assert len(incomplete) == 1
    assert f"CI run 1 (push on {campaign['base_branch']}, {baseline_status})" in incomplete[0]
    assert "run 2 " not in incomplete[0], "a finished baseline run must not be reported as pending"


def test_unfinished_run_outside_the_window_does_not_block_coverage(tmp_path):
    # Overshoot control for the test above: only in-window campaign runs gate
    # completion. A run created before the window opened is not campaign spend,
    # so one still in flight must not hold the campaign incomplete forever.
    campaign, runs, baseline_created = _prepared_campaign_with_runs(tmp_path)
    stale = _run_row(
        run_id=50,
        workflow="CI",
        created_at=_gh_created_at(baseline_created - timedelta(hours=2)),
        branch=campaign["base_branch"],
        event="push",
    )
    stale["status"] = "in_progress"
    stale["conclusion"] = None
    runs.append(stale)

    result = collect_campaign(campaign, _CollectListRunner(runs))

    assert 50 not in {row["databaseId"] for row in result["campaign_runs"]}
    assert result["complete"] is True
    assert result["anomalies"] == []


@pytest.mark.parametrize("missing_workflow", ["CI", "Build docs"])
def test_missing_baseline_run_keeps_coverage_incomplete(tmp_path, missing_workflow):
    # GitHub never created or returned one of the baseline push's runs. With
    # no row to report as unfinished, the snapshot must still refuse to claim
    # complete coverage, because that run's cost is absent (Codex P2 on PR #3943).
    campaign, runs, _baseline_created = _prepared_campaign_with_runs(tmp_path)
    runs = [row for row in runs if not (row.get("event") == "push" and row["workflowName"] == missing_workflow)]

    result = collect_campaign(campaign, _CollectListRunner(runs))

    assert all(workflow["status"] == "completed" for case in result["cases"] for workflow in case["workflows"]), (
        "fixture must have every PR workflow finished, or this test proves nothing"
    )
    assert result["complete"] is False
    missing = [item for item in result["anomalies"] if "baseline push" in item]
    assert missing == [
        f"Coverage incomplete: baseline push on {campaign['base_branch']} has no run for: {missing_workflow}"
    ]


def test_baseline_run_on_another_branch_does_not_count(tmp_path):
    # Overshoot control: a push run of the right workflow on some other branch
    # is not the baseline push, so it must not satisfy the requirement.
    campaign, runs, _baseline_created = _prepared_campaign_with_runs(tmp_path)
    for row in runs:
        if row.get("event") == "push" and row["workflowName"] == "CI":
            row["headBranch"] = "not-the-base"

    result = collect_campaign(campaign, _CollectListRunner(runs))

    assert result["complete"] is False
    assert any(item.endswith("has no run for: CI") for item in result["anomalies"])
