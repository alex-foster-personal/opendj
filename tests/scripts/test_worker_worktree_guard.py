"""Worker branch provenance and PR scope guard tests.

[if] a worker branch inherits unpublished primary commits [then] fail before review, [else stop].

Acceptance:
  - [if] the primary checkout is ahead of its origin base [then] preflight fails loudly.
  - [if] a worker worktree is created [then] it starts exactly at the requested base ref.
  - [if] a branch is much larger than its declared issue scope [then] scope check fails.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import worker_worktree_guard as guard

pytestmark = pytest.mark.requirement("OPS-41")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def origin_repo(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True)
    _git(checkout, "config", "user.email", "test@example.invalid")
    _git(checkout, "config", "user.name", "test")
    (checkout / "README.md").write_text("base\n")
    _git(checkout, "add", "README.md")
    _git(checkout, "commit", "-qm", "base")
    _git(checkout, "branch", "-M", "main")
    _git(checkout, "push", "-q", "origin", "main")
    _git(checkout, "fetch", "-q", "origin", "main")
    return origin, checkout


def test_preflight_refuses_primary_ahead_of_origin(origin_repo: tuple[Path, Path]) -> None:
    """[if] primary is ahead of origin/main [then] worker preflight refuses, [else stop]."""
    _, checkout = origin_repo
    (checkout / "local.txt").write_text("unpublished\n")
    _git(checkout, "add", "local.txt")
    _git(checkout, "commit", "-qm", "unpublished primary work")

    with pytest.raises(guard.PreflightError, match="ahead of origin/main"):
        guard.assert_clean_origin_base(checkout, "origin/main")


def test_preflight_refuses_source_local_base(origin_repo: tuple[Path, Path]) -> None:
    """[if] base is a local branch [then] worker preflight refuses, [else stop]."""
    _, checkout = origin_repo

    with pytest.raises(guard.PreflightError, match="source-local"):
        guard.assert_clean_origin_base(checkout, "main")


def test_create_worktree_starts_at_explicit_origin_base(
    origin_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    """[if] a worker worktree is created [then] HEAD equals origin/main, [else stop]."""
    _, checkout = origin_repo
    target = tmp_path / "worker"
    branch = "af--issue-3352-guard-test"

    guard.create_worker_worktree(checkout, target, branch, "origin/main")

    assert _git(target, "rev-parse", "HEAD") == _git(checkout, "rev-parse", "origin/main")
    assert _git(target, "branch", "--show-current").strip() == branch


def test_scope_check_refuses_large_branch(origin_repo: tuple[Path, Path]) -> None:
    """[if] branch exceeds declared scope [then] check fails before review, [else stop]."""
    _, checkout = origin_repo
    for index in range(3):
        (checkout / f"unrelated-{index}.txt").write_text("change\n")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "oversized change")

    result = guard.measure_scope(checkout, "origin/main")
    assert result.commits == 1
    assert result.files == 3
    with pytest.raises(guard.ScopeError, match="exceeds declared issue scope"):
        guard.assert_scope(result, max_commits=1, max_files=1)
