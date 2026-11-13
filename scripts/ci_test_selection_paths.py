"""Git diff paths for PR heads and main pushes."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.ci_test_selection_event import MAIN_RANGE_VARIABLE
from scripts.ci_test_selection_rules import FullSuite


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def main_push_base(root: Path, head_sha: str, record: dict) -> tuple[str, str]:
    """(base sha, why) for a main push: the range's green base, else HEAD's first parent.

    Raises FullSuite when the checkout is not the pushed commit: nothing it diffs is this push.
    """
    head = _git(root, "rev-parse", "HEAD")
    if head != head_sha:
        raise FullSuite(f"the checkout's HEAD is {head}, not the pushed commit {head_sha}")
    base = record.get("base")
    if isinstance(base, str) and base and record.get("head") == head_sha:
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base, head], cwd=root, capture_output=True, check=False
        )
        if ancestor.returncode == 0:
            return base, f"since {base[:10]}, the newest main commit with a green `ci gate`"
        problem = f"the range base {base[:10]} is not an ancestor of HEAD"
    else:
        problem = record.get("unknown") or f"{MAIN_RANGE_VARIABLE} is missing or names another head"
    return _git(root, "rev-parse", "HEAD^1"), f"HEAD's own change only ({problem})"


#: `--diff-filter` for the paths a change ADDS. With `--no-renames` a rename is a delete plus an add.
ADDED_FILTER = "--diff-filter=A"


def range_changed_paths(root: Path, base: str, *filters: str) -> list[str]:
    """Paths changed between `base` and the checked-out HEAD; `--no-renames` lists a rename's old path too."""
    diff = _git(root, "diff", "--name-only", "--no-renames", *filters, base, "HEAD")
    return [line for line in diff.splitlines() if line]


def pr_changed_paths(root: Path, head_sha: str, *filters: str) -> list[str]:
    """Paths the PR changes against its merge base, read from the PR merge commit checkout.

    `actions/checkout` on a pull_request checks out the test merge commit, whose second
    parent is the PR head. That is verified, not assumed: a checkout of anything else
    cannot say what the PR changed. `--no-renames` lists a rename's old path too.
    """
    second_parent = _git(root, "rev-parse", "HEAD^2")
    if second_parent != head_sha:
        raise FullSuite(
            f"the checkout's HEAD^2 is {second_parent}, not the PR head {head_sha}, so the "
            "PR's own change set cannot be read from it"
        )
    merge_base = _git(root, "merge-base", "HEAD^1", "HEAD^2")
    diff = _git(root, "diff", "--name-only", "--no-renames", *filters, merge_base, "HEAD^2")
    return [line for line in diff.splitlines() if line]
