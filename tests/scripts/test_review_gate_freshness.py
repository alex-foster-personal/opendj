"""A review-gate verdict must come from gate code at least as new as main's.

PR #1720, Thu 10 Sep 2026: `just review-triage 1720` ran #1720's own
pre-#1731 copy of scripts/review_coverage.py and printed a false PASS. These
tests drive REAL git repositories in tmp_path, a bare `origin` plus a clone,
through the unmodified production functions. No mocks: every git call is the
same subprocess path the gate runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.review_gate_freshness import (
    GATE_PATHSPEC,
    pin_remote_branch,
    require_gate_current,
)
from scripts.review_gh import TriageError


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(root: Path, rel: str, text: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(root, "add", rel)
    _git(root, "commit", "-q", "-m", f"touch {rel}")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """(maintainer, pr_checkout): main carries one gate commit, and the PR
    checkout is a separate clone branched from it."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    maintainer = tmp_path / "maintainer"
    subprocess.run(["git", "clone", "-q", str(origin), str(maintainer)], check=True)
    _git(maintainer, "config", "user.email", "t@example.invalid")
    _git(maintainer, "config", "user.name", "t")
    _git(maintainer, "switch", "-q", "-c", "main")
    _commit(maintainer, "scripts/review_coverage.py", "v1\n")
    _git(maintainer, "push", "-q", "origin", "main")
    pr = tmp_path / "pr"
    subprocess.run(["git", "clone", "-q", str(origin), str(pr)], check=True)
    _git(pr, "config", "user.email", "t@example.invalid")
    _git(pr, "config", "user.name", "t")
    _git(pr, "switch", "-q", "-c", "af--feature")
    _commit(pr, "apps/feature.py", "x\n")
    return maintainer, pr


def _main_tip(pr: Path) -> str:
    return pin_remote_branch(pr, "origin", "main")


def test_a_branch_cut_before_a_gate_fix_on_main_cannot_render_a_verdict(
    repos: tuple[Path, Path],
) -> None:
    """THE #1720 CASE: main gains a gate fix after the PR branched."""
    maintainer, pr = repos
    _commit(maintainer, "scripts/review_coverage.py", "v2 fixed\n")
    _git(maintainer, "push", "-q", "origin", "main")
    with pytest.raises(TriageError, match=r"STALE: it lacks \w+ touch scripts/review_coverage"):
        require_gate_current(pr, _main_tip(pr), GATE_PATHSPEC)


def test_the_same_branch_passes_once_it_contains_the_gate_fix(
    repos: tuple[Path, Path],
) -> None:
    """Control for the test above, on the same repositories: merging main in
    must clear it, or refusing every run would pass that test trivially."""
    maintainer, pr = repos
    fix = _commit(maintainer, "scripts/review_coverage.py", "v2 fixed\n")
    _git(maintainer, "push", "-q", "origin", "main")
    tip = _main_tip(pr)
    _git(pr, "merge", "-q", "--no-edit", tip)
    assert require_gate_current(pr, tip, GATE_PATHSPEC) == fix


def test_main_moving_on_in_non_gate_files_does_not_block(
    repos: tuple[Path, Path],
) -> None:
    """The overshoot control: a checkout holding every gate commit is as
    trustworthy as main's gate even when main gained unrelated commits."""
    maintainer, pr = repos
    gate = _git(maintainer, "rev-parse", "HEAD")
    _commit(maintainer, "apps/unrelated.py", "y\n")
    _git(maintainer, "push", "-q", "origin", "main")
    assert require_gate_current(pr, _main_tip(pr), GATE_PATHSPEC) == gate


def test_a_pathspec_matching_nothing_is_a_failed_measurement(
    repos: tuple[Path, Path],
) -> None:
    """Negative control on the instrument: a renamed gate directory must not
    make freshness pass vacuously."""
    _, pr = repos
    with pytest.raises(TriageError, match="cannot be measured"):
        require_gate_current(pr, _main_tip(pr), ("nowhere/review_*.py",))


def test_a_remote_without_the_branch_is_a_failed_measurement(
    repos: tuple[Path, Path],
) -> None:
    _, pr = repos
    with pytest.raises(TriageError, match="advertises no refs/heads/nope"):
        pin_remote_branch(pr, "origin", "nope")


def test_pinning_main_does_not_write_fetch_head(repos: tuple[Path, Path]) -> None:
    """FETCH_HEAD is shared across every agent's worktree in this repo."""
    _, pr = repos
    fetch_head = Path(_git(pr, "rev-parse", "--git-path", "FETCH_HEAD"))
    fetch_head = fetch_head if fetch_head.is_absolute() else pr / fetch_head
    before = fetch_head.read_bytes() if fetch_head.exists() else None
    _main_tip(pr)
    after = fetch_head.read_bytes() if fetch_head.exists() else None
    assert before == after
