"""How :mod:`scripts.adr_pr_lookup` finds the open PR for a local checkout.

``gh pr view --repo <repo>`` with no argument is rejected by gh ("argument required when
using the --repo flag"), which made ``current_pr_number`` return None on every local run
and sent ``just pre-push`` to the commit-message fallback even when the PR body carried
the ADR declaration (reproduced on #3547). These tests run against a real throwaway git
repo and the real ``subprocess.run`` for git. ``gh`` is answered in-process by a model of
its two real replies, so the suite needs no gh binary, network, or GitHub login: runners
without gh (bifrost1-wsl, PR #3940 shard 4) raised FileNotFoundError here.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts import adr_pr_lookup as mod

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _gh_pr_view_reply(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Model gh's real `gh pr view [<branch>] --repo R` replies: no branch rejected, else no PR."""
    branch_given = len(args) > 3 and not args[3].startswith("-")
    if branch_given:
        return subprocess.CompletedProcess(
            args, 1, "", f'no pull requests found for branch "{args[3]}"\n'
        )
    return subprocess.CompletedProcess(
        args, 1, "", "argument required when using the --repo flag\n"
    )


def _recording_runner() -> tuple[list[list[str]], Runner]:
    """Real subprocess.run for git, a gh model for gh; every argv is recorded for assertions."""
    calls: list[list[str]] = []

    def recording_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[:3] == ["gh", "pr", "view"]:
            return _gh_pr_view_reply(list(args))
        if args[0] == "gh":
            raise AssertionError(f"unmodelled gh call, would need a real gh: {args}")
        return subprocess.run(args, **kwargs)  # noqa: PLW1510 -- callers pass check=False

    return calls, recording_run


def _git_repo_on_branch(tmp_path: Path, branch: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (
        ["git", "init", "-q", "-b", branch],
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "c",
        ],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    return repo


def test_current_pr_number_passes_branch_to_gh_when_repo_given(tmp_path: Path) -> None:
    """[if] gh pr view gets --repo without a branch [then] gh rejects it and no PR is ever found."""
    branch = "adr-check-no-such-pr-branch-3547"
    repo = _git_repo_on_branch(tmp_path, branch)
    calls, recording_run = _recording_runner()

    number = mod.current_pr_number(repo, "maintainer/music-dj-tools", run=recording_run)

    gh_calls = [argv for argv in calls if argv[:3] == ["gh", "pr", "view"]]
    assert len(gh_calls) == 1
    argv = gh_calls[0]
    assert "--repo" in argv
    assert argv[3] == branch, f"gh pr view got no branch before --repo: {argv}"
    # No PR exists for this branch, so the caller must fall back to commit messages.
    assert number is None


def test_current_pr_number_skips_gh_on_detached_head(tmp_path: Path) -> None:
    """[if] a detached HEAD still queries gh [then] it asks about an empty branch name."""
    repo = _git_repo_on_branch(tmp_path, "main")
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=repo, check=True)
    calls, recording_run = _recording_runner()

    assert mod.current_pr_number(repo, run=recording_run) is None
    assert not [argv for argv in calls if argv[0] == "gh"]
