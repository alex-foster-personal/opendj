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
from scripts import worktree_lifecycle

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


def test_preflight_refuses_dirty_working_tree(origin_repo: tuple[Path, Path]) -> None:
    """[if] the primary checkout has uncommitted changes [then] worker preflight
    refuses with a named reason, [else stop].

    This is the half of R-1 that PR #3359's own tests never exercised: both
    existing checks above cover "ahead of origin" and "source-local base", but
    nothing dirtied the working tree without also committing. Mutation testing
    (issue #3352 follow-on, PR #3927) confirmed disabling the dirty branch of
    `assert_clean_origin_base` left every prior test green.
    """
    _, checkout = origin_repo
    (checkout / "untracked.txt").write_text("uncommitted\n")

    with pytest.raises(guard.PreflightError, match="dirty"):
        guard.assert_clean_origin_base(checkout, "origin/main")


def test_preflight_passes_clean_checkout_at_origin(origin_repo: tuple[Path, Path]) -> None:
    """[if] the primary checkout is clean and exactly at origin/main [then]
    preflight raises nothing, [else stop].

    Overshoot control for the dirty-checkout test above: a guard that refuses
    every checkout (or refuses on any file existing) would also make this test
    fail, so a passing dirty-check needs this control alongside it, not instead
    of it.
    """
    _, checkout = origin_repo

    guard.assert_clean_origin_base(checkout, "origin/main")


def test_preflight_refuses_source_local_base(origin_repo: tuple[Path, Path]) -> None:
    """[if] base is a local branch [then] worker preflight refuses, [else stop]."""
    _, checkout = origin_repo

    with pytest.raises(guard.PreflightError, match="source-local"):
        guard.assert_clean_origin_base(checkout, "main")


def test_create_worktree_starts_at_explicit_origin_base(
    origin_repo: tuple[Path, Path], tmp_path: Path
) -> None:
    """[if] a worker worktree is created [then] HEAD equals origin/main, [else stop].

    This test is about the starting ref, not disk space, so it pins
    `floor_gb=0`: `create_worker_worktree` -> the lifecycle guard's
    `cmd_guard` reads `shutil.disk_usage` on the real HOST, not on anything
    this test controls, and a host with little real free space (nucbox
    runners with RUNNER_TEMP on a small tmpfs, CI job 109075514456, run
    36465719472) made this fail with "disk floor breached" despite the
    worktree logic itself being correct. See
    test_create_worktree_still_refuses_below_the_disk_floor below for the
    control that proves this override does not disable the guard.

    Regression lines:
      - if the worker worktree HEAD differs from origin/main then broken
      - if this test's verdict changes with the host's free disk space then broken
    """
    _, checkout = origin_repo
    target = tmp_path / "worker"
    branch = "af--issue-3352-guard-test"

    guard.create_worker_worktree(checkout, target, branch, "origin/main", floor_gb=0)

    assert _git(target, "rev-parse", "HEAD") == _git(checkout, "rev-parse", "origin/main")
    assert _git(target, "branch", "--show-current").strip() == branch


def test_create_worktree_still_refuses_below_the_disk_floor(
    origin_repo: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] real free disk is below the floor [then] worktree creation still
    refuses, [else stop].

    The ONE disk-floor test on the worker create path, and the control for
    the floor_gb override above: proves the fix is "inject the floor", not
    "disable the guard". It reads the REAL free space of the same path the
    guard measures (`--repo`, here `checkout`) and sets the floor 1000G above
    it, so the live measurement path is exercised with no monkeypatch and the
    verdict cannot depend on how full the host disk is.

    Regression lines:
      - if a floor above real free space lets creation through then broken
      - if the refusal does not name the floor value it compared against then broken
      - if the refusal does not name the reap command (`just wt-reap`) then broken
      - if a refused creation still leaves a worktree on disk then broken
    """
    _, checkout = origin_repo
    target = tmp_path / "worker"
    floor_above_real_free = worktree_lifecycle.free_gb(checkout) + 1000

    with pytest.raises(guard.PreflightError, match="worktree lifecycle guard refused"):
        guard.create_worker_worktree(
            checkout, target, "af--disk-floor-refusal", "origin/main",
            floor_gb=floor_above_real_free,
        )

    err = capsys.readouterr().err
    assert "disk floor breached:" in err
    assert f"floor {floor_above_real_free:.0f}G" in err
    assert "just wt-reap" in err
    assert not target.exists()


def test_create_cli_offers_no_disk_floor_override() -> None:
    """[if] the worker `create` CLI is given --floor-gb [then] it is rejected,
    [else stop]: production callers always get the lifecycle floor policy."""
    with pytest.raises(SystemExit):
        guard.main(["create", "--repo", ".", "--target", "x", "--branch", "b",
                    "--base", "origin/main", "--floor-gb", "0"])


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
