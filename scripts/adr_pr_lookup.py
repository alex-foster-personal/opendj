"""GitHub PR lookups for :mod:`scripts.adr_check`: the PR body, its files, and which PR is open.

``gh pr view --repo <repo>`` cannot infer a PR from the checkout, so
:func:`current_pr_number` always passes the current branch. A detached HEAD or a branch
with no open PR yields None, and the caller falls back to commit messages.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DEFAULT_REPO = "maintainer/music-dj-tools"


def pr_view(pr: int, repo: str = DEFAULT_REPO) -> dict:
    proc = subprocess.run(
        ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "number,title,body"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr view failed rc={proc.returncode}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def pr_files(pr: int, repo: str = DEFAULT_REPO) -> list[str]:
    """Every path the PR changes. Paginated, because ``gh pr view --json files`` truncates."""
    proc = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/pulls/{pr}/files",
            "--jq",
            ".[].filename",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh api pulls/{pr}/files failed rc={proc.returncode}: {proc.stderr.strip()}"
        )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def current_branch(repo_root: Path, run=subprocess.run) -> str | None:
    """The checked-out branch, or None on a detached HEAD (a CI merge ref)."""
    proc = run(
        ["git", "branch", "--show-current"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git branch --show-current failed rc={proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip() or None


def current_pr_number(repo_root: Path, repo: str = DEFAULT_REPO, run=subprocess.run) -> int | None:
    """Open PR for the current branch, or None so the caller falls back to commit messages.

    ``gh pr view --repo`` requires an explicit branch (it cannot infer one from the
    checkout once ``--repo`` is given), so the branch is always passed.
    """
    branch = current_branch(repo_root, run)
    if branch is None:
        return None
    proc = run(
        ["gh", "pr", "view", branch, "--repo", repo, "--json", "number"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    number = payload.get("number")
    return int(number) if number else None
