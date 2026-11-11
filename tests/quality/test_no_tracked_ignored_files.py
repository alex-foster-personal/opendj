"""Guard: no newly tracked file matches a .gitignore pattern.

A generated file that is gitignored but still tracked (reqs.json, coverage-matrix.md)
broke main whenever a generator rewrote it. Any push path can introduce one, so this
scans the whole index instead of trusting a pre-push hook. Files force-added on purpose
are baselined in fixtures/tracked_ignored_baseline.txt, which only shrinks. The checks
live in scripts/ci_tree_hygiene.py, which ci.yml's scope job also runs on every pull request.

[if] git ls-files -ci --exclude-per-directory=.gitignore lists a path missing from the baseline [then]
this test fails and names each path with the `git rm --cached` fix, [else stop].

Regression lines:
  - if a gitignored file is tracked and not baselined then this test fails
  - if a baselined path is no longer tracked-and-ignored then this test fails (stale line)
  - if the test runs outside a git checkout then it skips with a reason, never passes
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci_tree_hygiene import TRACKED_IGNORED_BASELINE, new_tracked_ignored, stale_tracked_ignored_baseline

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def _require_git_checkout() -> None:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip(f"not a git checkout: {REPO_ROOT} has no .git, cannot list tracked files")


def test_no_new_tracked_file_is_gitignored() -> None:
    """[if] a gitignored path is tracked and not baselined [then] fail listing it, [else stop]."""
    _require_git_checkout()
    new_paths = new_tracked_ignored(REPO_ROOT)
    assert not new_paths, (
        f"{len(new_paths)} tracked file(s) match a .gitignore pattern:\n"
        + "\n".join(f"  {p}" for p in new_paths)
        + "\nFix: git rm --cached <path> for each (keeps the local copy), then commit."
    )


def test_tracked_ignored_baseline_has_no_stale_lines() -> None:
    """[if] a baselined path is no longer tracked-and-ignored [then] fail naming it, [else stop]."""
    _require_git_checkout()
    stale = stale_tracked_ignored_baseline(REPO_ROOT)
    assert not stale, (
        f"{len(stale)} stale line(s) in {TRACKED_IGNORED_BASELINE.name}:\n"
        + "\n".join(f"  {p}" for p in stale)
        + "\nFix: delete those lines from the baseline."
    )
