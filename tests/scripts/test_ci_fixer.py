"""Production-boundary checks for the ci-fixer lane (issue #1017)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import ci_fixer, ci_health_core
from scripts import ci_fixer_core as cf


def _job_dict(
    run_id=1, job_id=1, pr_number=7, runner_name="agentbox-2", job_name="pytest fast lane"
):
    return {
        "run_id": run_id,
        "job_id": job_id,
        "job_name": job_name,
        "pr_number": pr_number,
        "head_sha": "b" * 40,
        "runner_name": runner_name,
        "workflow": "ci.yml",
        "html_url": f"https://github.com/{cf.REPO}/actions/runs/{run_id}",
        "log_excerpt": "AssertionError: assert 1 == 2",
        "created_at": "2026-09-03T00:00:00Z",
    }


FAILED_STEP_LOG = "\n".join(
    [
        "job\tUNKNOWN\t2026-09-03T18:46:21.9946720Z ##[group]Run pytest \\",
        "job\tUNKNOWN\t2026-09-03T18:46:21.9946980Z pytest \\",
        "job\tUNKNOWN\t2026-09-03T18:46:21.9947179Z   --collect-floor 3150 \\",
        "job\tUNKNOWN\t2026-09-03T18:46:21.9947420Z   --ignore=tests/analysis",
        "job\tUNKNOWN\t2026-09-03T18:46:21.9961954Z shell: /usr/bin/bash -e {0}",
        "job\tUNKNOWN\t2026-09-03T18:57:51.0905789Z ##[error]Process completed with exit code 1.",
    ]
)


def test_extract_failed_step_command_preserves_the_ci_collect_floor_and_ignores():
    command = ci_fixer.extract_failed_step_command(FAILED_STEP_LOG)
    assert command == [
        "pytest",
        "--collect-floor",
        "3150",
        "--ignore=tests/analysis",
    ]


def test_extract_failed_step_command_refuses_to_invent_a_scoped_recheck():
    assert ci_fixer.extract_failed_step_command("FAILED tests/x.py::test_y") is None


def test_find_recheck_working_directory_uses_the_step_setting(tmp_path):
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """jobs:
  frontend:
    steps:
      - name: check
        working-directory: apps/webui/frontend
        run: pnpm exec svelte-check
""",
        encoding="utf-8",
    )
    assert ci_fixer.find_recheck_working_directory(
        ["pnpm", "exec", "svelte-check"], tmp_path
    ) == Path("apps/webui/frontend")


def test_worktree_branch_name_is_reusable_after_cleanup():
    assert ci_fixer.worktree_branch_name(_job_dict(run_id=17)["run_id"]) == "ci-fixer/run-17"


# ----- _root_cause_summary -------------------------------------------------------------


def test_root_cause_summary_takes_first_two_nonempty_lines():
    msg = "\nOff-by-one in the retry loop.\nFixed by using >= instead of >.\nextra line\n"
    assert ci_fixer._root_cause_summary(msg) == (
        "Off-by-one in the retry loop. Fixed by using >= instead of >."
    )


def test_root_cause_summary_empty_message():
    assert ci_fixer._root_cause_summary("") == "worker produced no summary"


# ----- _build_prompt: guard rails must be spelled out to the worker itself -------------


def test_build_prompt_names_every_guarded_prefix_and_forbids_pushing():
    job = cf.FailedJob(**_job_dict())
    prompt = ci_fixer._build_prompt(job, "log", "diff")
    for prefix in cf.GUARDED_PATH_PREFIXES:
        assert prefix in prompt
    assert "do not push" in prompt.lower()
    assert "fabricate" in prompt.lower()


def _run(command: list[str], cwd) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def test_capture_diff_includes_an_untracked_module_from_a_real_git_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "ci-fixer@example.com"], repo)
    _run(["git", "config", "user.name", "ci-fixer"], repo)
    (repo / "tracked.py").write_text("from helper import value\n", encoding="utf-8")
    _run(["git", "add", "tracked.py"], repo)
    _run(["git", "commit", "-qm", "initial"], repo)
    (repo / "helper.py").write_text("value = 2\n", encoding="utf-8")

    diff_text = ci_fixer.capture_diff(repo)

    assert "diff --git a/helper.py b/helper.py" in diff_text
    assert "+value = 2" in diff_text


def test_capture_diff_excludes_the_worker_transcript_from_a_real_git_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "ci-fixer@example.com"], repo)
    _run(["git", "config", "user.name", "ci-fixer"], repo)
    (repo / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    _run(["git", "add", "tracked.py"], repo)
    _run(["git", "commit", "-qm", "initial"], repo)
    (repo / ci_fixer.WORKER_TRANSCRIPT_FILENAME).write_text("worker output\n", encoding="utf-8")

    assert ci_fixer.capture_diff(repo) == ""


def test_run_recheck_terminates_a_real_hanging_subprocess(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        ci_fixer.run_recheck(
            ["/usr/bin/python3", "-c", "import time; time.sleep(1)"],
            tmp_path,
            Path("."),
            timeout_seconds=0.01,
        )


def test_run_recheck_hides_host_credentials_and_files_from_pr_controlled_code(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    host_secret = tmp_path / "persistent-credential"
    host_secret.write_text("do-not-expose", encoding="utf-8")
    monkeypatch.setenv("CI_FIXER_PERSISTENT_CREDENTIAL", "do-not-expose")

    result = ci_fixer.run_recheck(
        [
            "/usr/bin/python3",
            "-c",
            (
                "import os, pathlib; "
                f"print(os.environ.get('CI_FIXER_PERSISTENT_CREDENTIAL', 'hidden'), "
                f"pathlib.Path({str(host_secret)!r}).exists())"
            ),
        ],
        worktree,
        Path("."),
    )

    assert result.returncode == 0
    assert result.stdout == "hidden False\n"


def test_fetch_pr_head_sha_refuses_a_stale_proposal_before_any_post(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"headRefOid\": \"advanced\"}'\n", encoding="utf-8"
    )
    fake_gh.chmod(0o755)
    monkeypatch.setattr(ci_health_core, "GH_BIN", str(fake_gh))
    outcome = cf.FixOutcome(
        job=cf.FailedJob(**_job_dict()),
        disposition="fix-proposed",
        reason="verified old head",
        diff_text="diff --git a/a b/a\n",
        comment_body="proposal",
    )

    with pytest.raises(ci_fixer.PreconditionError, match="advanced"):
        ci_fixer.post_outcome(outcome)


def test_claim_job_is_atomic_at_the_ledger_boundary_and_recovers_an_expired_lease(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    job = cf.FailedJob(**_job_dict(run_id=10))

    assert cf.claim_job(job, ledger_path) == "claimed"
    assert cf.claim_job(job, ledger_path) == "seen"
    with cf.ledger_transaction(ledger_path) as ledger:
        ledger["entries"][job.dedupe_key]["lease_expires_at"] = 0
    assert cf.claim_job(job, ledger_path) == "claimed"


def test_release_claim_makes_a_transient_worker_or_github_failure_retryable(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    job = cf.FailedJob(**_job_dict(run_id=11))

    assert cf.claim_job(job, ledger_path) == "claimed"
    cf.release_claim(job, ledger_path)

    assert cf.claim_job(job, ledger_path) == "claimed"


def test_finalize_requires_an_active_claim_and_then_dedupes(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    job = cf.FailedJob(**_job_dict(run_id=12))
    outcome = cf.FixOutcome(job=job, disposition="diagnosis-only", reason="no repro")

    with pytest.raises(cf.PreconditionError):
        cf.finalize_outcome(outcome, ledger_path)
    assert cf.claim_job(job, ledger_path) == "claimed"
    cf.finalize_outcome(outcome, ledger_path)

    assert cf.claim_job(job, ledger_path) == "seen"
