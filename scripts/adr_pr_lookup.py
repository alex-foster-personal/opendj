"""GitHub PR lookups for :mod:`scripts.adr_check`: the PR body, its files, and which PR is open.

``gh pr view --repo <repo>`` cannot infer a PR from the checkout, so
:func:`current_pr_number` always passes the current branch. A detached HEAD or a branch
with no open PR yields None, and the caller falls back to commit messages.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

DEFAULT_REPO = "private_owner/music-dj-tools"


def pr_view(pr: int, repo: str = DEFAULT_REPO) -> dict:
    proc = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "number,title,body,headRefName,author,isCrossRepository",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr view failed rc={proc.returncode}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


# Trunk Merge Queue tests a batch on a PR it opens itself, head ``trunk-merge/pr-<N>/<uuid>``
# (``-bisection`` suffix when splitting a failed batch). Its body is Trunk's banner, never an
# ADR declaration, so the batch is judged by its member PRs' own declarations instead.
# Anyone can name a branch ``trunk-merge/...`` and paste a member list into their body, so
# only a same-repo PR authored by the Trunk GitHub App (an identity no contributor can hold)
# is treated as a batch. Anything else is judged on its own body.
TRUNK_BATCH_HEAD_RE = re.compile(r"^trunk-(?:merge|temp)/pr-\d+/")
TRUNK_APP_LOGIN = "app/trunk-io"
_TESTED_SECTION = "## Pull Requests Being Tested"
_MEMBER_LINK_RE = re.compile(r"github\.com/private_owner/music-dj-tools/pull/(\d+)")


def trunk_batch_members(pr: dict) -> list[int] | None:
    """Member PR numbers of a Trunk batch PR, or None when ``pr`` is not a Trunk batch.

    A batch whose members cannot be read raises, so the caller reports UNKNOWN rather
    than judging an empty body.
    """
    authored_by_trunk = (pr.get("author") or {}).get("login") == TRUNK_APP_LOGIN
    same_repo = pr.get("isCrossRepository") is False
    batch_head = TRUNK_BATCH_HEAD_RE.match(pr.get("headRefName") or "")
    if not (batch_head and authored_by_trunk and same_repo):
        return None
    _, found, section = (pr.get("body") or "").partition(_TESTED_SECTION)
    members = list(dict.fromkeys(int(n) for n in _MEMBER_LINK_RE.findall(section)))
    if not found or not members:
        raise RuntimeError(
            f"Trunk batch PR #{pr.get('number')} lists no member PRs under {_TESTED_SECTION!r}"
        )
    return members


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
