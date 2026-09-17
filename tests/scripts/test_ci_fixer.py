"""Production-boundary checks for the ci-fixer lane (issue #1017)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import ci_fixer, ci_health_core
from scripts import ci_fixer_core as cf
from scripts.ci_fixer_pr_diff import (
    _render_file_diff,
    _validate_file_records,
    fetch_pr_diff_with_fallback,
)
from scripts.ci_health_core import REPO, PreconditionError


def _require_bwrap() -> None:
    """The credential-free sandbox cannot run without bubblewrap; say so.

    `run_recheck` wraps its command in `bwrap` and raises PreconditionError
    when the binary is absent, which is correct production behavior but, let
    loose in a test, turns a runner that merely lacks the tool into a red that
    looks like the sandbox regressed. The self-hosted fleet is inconsistent
    here (measured: `bwrap is required for credential-free rechecks` on main
    run dad4ef330). A host without bwrap cannot exercise the sandbox at all, so
    the honest verdict is UNAVAILABLE, matching the sshd fixture next door.
    """
    if shutil.which("bwrap") is None:
        pytest.skip(
            "UNAVAILABLE: bwrap (bubblewrap) is not installed, so the "
            "credential-free recheck sandbox cannot run on this host"
        )


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
    _require_bwrap()
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
    _require_bwrap()
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


def test_run_recheck_terminate_skips_when_bwrap_is_unavailable(tmp_path, monkeypatch):
    """The sandbox tests report UNAVAILABLE, not PreconditionError, sans bwrap.

    Each regression drives the REAL test function with `bwrap` hidden from
    PATH: the guard must convert the absence into a skip. If someone removes
    the guard from `test_run_recheck_terminates_a_real_hanging_subprocess`,
    this fails - `run_recheck` raises PreconditionError instead of skipping.
    """
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(pytest.skip.Exception, match="UNAVAILABLE"):
        test_run_recheck_terminates_a_real_hanging_subprocess(tmp_path)


def test_run_recheck_hides_skips_when_bwrap_is_unavailable(tmp_path, monkeypatch):
    """Same guard regression for the credential-hiding sandbox test."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(pytest.skip.Exception, match="UNAVAILABLE"):
        test_run_recheck_hides_host_credentials_and_files_from_pr_controlled_code(
            tmp_path, monkeypatch
        )


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


# ----- fetch_pr_diff: normal path and >300-file fallback (issue #2546) ---------------


def test_fetch_pr_diff_returns_normal_unified_diff_unchanged(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []
    expected = "diff --git a/foo.py b/foo.py\n+print('ok')\n"

    def fake_run_gh(args: list[str]) -> str:
        calls.append(args)
        return expected

    monkeypatch.setattr(ci_fixer, "_run_gh", fake_run_gh)

    assert ci_fixer.fetch_pr_diff(42) == expected
    assert calls == [["pr", "diff", "42"]]


def test_fetch_pr_diff_reconstructs_context_when_diff_exceeds_file_cap(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[list[str]] = []
    cap_error = PreconditionError(
        "gh pr diff 2502 failed with exit 1: HTTP 406: "
        "the diff exceeded the maximum number of files"
    )

    records: list[dict[str, object]] = [
        {
            "filename": "first.py",
            "status": "modified",
            "additions": 1,
            "deletions": 0,
            "changes": 1,
            "patch": "@@ -1 +1 @@\n-old\n+new",
        },
        {
            "filename": "binary.dat",
            "status": "modified",
            "additions": 0,
            "deletions": 0,
            "changes": 0,
        },
    ]
    records.extend(
        {
            "filename": f"file-{index:03d}.py",
            "status": "modified",
            "additions": 1,
            "deletions": 0,
            "changes": 1,
            "patch": f"@@ file {index} @@",
        }
        for index in range(3, 305)
    )
    records.append(
        {
            "filename": "last.py",
            "status": "added",
            "additions": 3,
            "deletions": 0,
            "changes": 3,
            "patch": "@@ -0,0 +1,3 @@\n+line",
        }
    )
    page_one = records[:200]
    page_two = records[200:]
    slurped = json.dumps([page_one, page_two])

    def fake_run_gh(args: list[str]) -> str:
        calls.append(args)
        if args[:2] == ["pr", "diff"]:
            raise cap_error
        assert args[:2] == ["api", f"repos/{REPO}/pulls/2502/files?per_page=100"]
        assert args[2:] == ["--paginate", "--slurp"]
        return slurped

    monkeypatch.setattr(ci_fixer, "_run_gh", fake_run_gh)

    result = ci_fixer.fetch_pr_diff(2502)

    assert calls[0] == ["pr", "diff", "2502"]
    assert calls[1][0:2] == ["api", f"repos/{REPO}/pulls/2502/files?per_page=100"]
    assert len(calls) == 2
    assert "diff --git a/first.py b/first.py" in result
    assert "@@ -1 +1 @@" in result
    assert "diff --git a/binary.dat b/binary.dat" in result
    assert "[patch unavailable from GitHub files API" in result
    assert "diff --git a/dev/null b/last.py" in result
    assert "@@ -0,0 +1,3 @@" in result
    assert result.count("diff --git ") == 305


def test_fetch_pr_diff_reraises_non_cap_failures_without_files_api(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[list[str]] = []

    def fake_run_gh(args: list[str]) -> str:
        calls.append(args)
        raise PreconditionError("gh pr diff 9 failed with exit 1: HTTP 404: Not Found")

    monkeypatch.setattr(ci_fixer, "_run_gh", fake_run_gh)

    with pytest.raises(PreconditionError, match="HTTP 404"):
        ci_fixer.fetch_pr_diff(9)
    assert calls == [["pr", "diff", "9"]]


def test_render_file_diff_marks_added_deleted_and_renamed_paths():
    added = _render_file_diff({"filename": "new.py", "status": "added", "patch": "+x"})
    deleted = _render_file_diff({"filename": "gone.py", "status": "removed"})
    renamed = _render_file_diff(
        {
            "filename": "new.py",
            "previous_filename": "old.py",
            "status": "renamed",
            "patch": "@@ rename @@",
        }
    )

    assert "--- /dev/null" in added
    assert "+++ b/new.py" in added
    assert "--- a/gone.py" in deleted
    assert "+++ /dev/null" in deleted
    assert "[patch unavailable" in deleted
    assert "diff --git a/old.py b/new.py" in renamed


def test_validate_file_records_rejects_malformed_payload():
    with pytest.raises(PreconditionError, match="not a list of pages"):
        _validate_file_records({"filename": "x"}, pr_number=1)
    with pytest.raises(PreconditionError, match="invalid filename"):
        _validate_file_records([[{"filename": ""}]], pr_number=1)
    with pytest.raises(PreconditionError, match="no records"):
        _validate_file_records([[]], pr_number=1)


def test_fetch_pr_diff_with_fallback_uses_injected_runner():
    seen: list[list[str]] = []

    def runner(args: list[str]) -> str:
        seen.append(args)
        return "diff --git a/x b/x\n"

    assert fetch_pr_diff_with_fallback(3, REPO, runner) == "diff --git a/x b/x\n"
    assert seen == [["pr", "diff", "3"]]
