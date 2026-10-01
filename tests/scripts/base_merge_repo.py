"""Throwaway git repositories for the base-merge carry tests (REVIEW-12).

Real commits, real merges and a real bare `origin` whose main is pinned by
`ls-remote`. No mocks of git.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.review_claude import CLAUDE
from scripts.review_claude import marker as claude_marker
from scripts.review_coverage_carry import CarryAttempt, carry_attempt

PR = "4426"
FILE = "apps/foo.py"
OTHER = "apps/other.py"
THIRD = "apps/third.py"
QUOTED = "apps/café dir/my file.py"
LOGIN = "maintainer"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def lines(edits: dict[int, str] | None = None) -> str:
    rows = [f"line{i}" for i in range(40)]
    for index, text in (edits or {}).items():
        rows[index] = text
    return "\n".join(rows) + "\n"


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(root, "add", rel)


def commit(root: Path, rel: str, text: str) -> str:
    write(root, rel, text)
    git(root, "commit", "-q", "-m", f"touch {rel}")
    return git(root, "rev-parse", "HEAD")


def advance_main(root: Path, rel: str, text: str) -> None:
    """Land a commit on origin's main, then return to the PR branch."""
    git(root, "checkout", "-q", "main")
    commit(root, rel, text)
    git(root, "push", "-q", "origin", "main")
    git(root, "checkout", "-q", "pr")


def merge_main(root: Path) -> str:
    git(root, "merge", "-q", "--no-edit", "main")
    return git(root, "rev-parse", "HEAD")


def merge_main_with_edit(root: Path, rel: str, text: str) -> str:
    """Merge main, then hand-edit `rel` inside the merge commit itself (an evil merge)."""
    subprocess.run(["git", "-C", str(root), "merge", "-q", "--no-commit", "main"], capture_output=True, check=False)
    write(root, rel, text)
    git(root, "commit", "-q", "--no-edit")
    head = git(root, "rev-parse", "HEAD")
    assert len(git(root, "rev-list", "--parents", "-n", "1", head).split()) == 3, "must be a 2-parent merge"
    return head


def make_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    git(work, "config", "user.email", "t@example.invalid")
    git(work, "config", "user.name", "t")
    git(work, "checkout", "-q", "-b", "main")
    commit(work, FILE, lines())
    commit(work, OTHER, "other v1\n")
    commit(work, THIRD, "third v1\n")
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "-b", "pr")
    return work


def make_reviewed(repo: Path) -> str:
    """The PR's own change, reviewed by Claude at this head."""
    return commit(repo, FILE, lines({30: "line30 pr"}))


def review_at(sha: str) -> dict:
    return {
        "user": {"login": LOGIN},
        "body": f"findings\n{claude_marker(sha, 'claude-test')}",
        "state": "COMMENTED",
        "commit_id": sha,
    }


def attempt(repo: Path, reviewed_sha: str, head: str) -> CarryAttempt:
    return carry_attempt(CLAUDE, PR, head, [review_at(reviewed_sha)], [], [], repo)


def assert_no_carry(repo: Path, reviewed_sha: str, head: str) -> None:
    result = attempt(repo, reviewed_sha, head)
    assert result.verdict is None and result.unknown == (), result
