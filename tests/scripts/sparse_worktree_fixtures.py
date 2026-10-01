"""Throwaway repositories for the sparse-worktree tests (OPS-45).

Every repository is real git, and the hook is the real
`scripts/githooks/post-checkout-sparse-worktree.sh` wired through
`core.hooksPath`, dispatching to the real `scripts/sparse_worktree.py` copied
into the fixture's own tree -- exactly how a live worktree runs it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "githooks" / "post-checkout-sparse-worktree.sh"
MODULE = ROOT / "scripts" / "sparse_worktree.py"

# Media the patterns must leave out, media they must keep, and prose beside both.
EXCLUDED = (
    "blog/next-hero-set/hero.png",
    "blog/hero-brainstorm/sketch.jpg",
    "docs/landscape/01-session/shot.jpeg",
    "docs/landscape/01-session/nested/deeper.png",
)
KEPT = (
    "blog/next-hero-set/README.md",
    "blog/post.md",
    "docs/landscape/01-session/notes.md",
    "docs/assets/diagram.png",
    "apps/web/icon.png",
    "apps/web/module.py",
    "uv.lock",
    "scripts/__init__.py",
    "scripts/sparse_worktree.py",
)
# A CGNAT address inside an excluded image: the publication audit must still find it.
LEAK_PATH = "blog/next-hero-set/hero.png"
LEAK_TEXT = b"render notes for 100.101.102.103\n"
# Prose linking INTO the excluded media, plus one genuinely dangling link as the
# control that the link probe can still say "dangling" in both trees.
LINKING_DOC = "docs/landscape/01-session/notes.md"
LINKING_TEXT = b"![shot](shot.jpeg) [deeper](nested/deeper.png) [gone](missing.png)\n"
CONTENT = {LEAK_PATH: LEAK_TEXT, LINKING_DOC: LINKING_TEXT}


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )
    assert done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr}"
    return done.stdout + done.stderr


def build_primary(tmp_path: Path) -> Path:
    """A primary checkout with the hook installed the way `wt-sparse-hook-install` does."""
    primary = tmp_path / "primary"
    subprocess.run(["git", "init", "-q", "-b", "main", str(primary)], check=True)
    git(primary, "config", "user.email", "test@example.invalid")
    git(primary, "config", "user.name", "test")
    for rel in (*EXCLUDED, *KEPT):
        path = primary / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(CONTENT.get(rel, f"content of {rel}\n".encode()))
    shutil.copyfile(MODULE, primary / "scripts" / "sparse_worktree.py")
    (primary / "scripts" / "__init__.py").write_text("")
    git(primary, "add", "-A")
    git(primary, "commit", "-qm", "fixture")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copyfile(HOOK, hooks / "post-checkout")
    (hooks / "post-checkout").chmod(0o755)
    git(primary, "config", "core.hooksPath", str(hooks))
    return primary


def add_worktree(primary: Path, target: Path, *, env: dict[str, str] | None = None) -> str:
    """Plain `git worktree add`, the creation path the hook must cover. Returns git's output."""
    return git(primary, "worktree", "add", "-q", str(target), "-b", target.name, env=env)


def on_disk(repo: Path) -> set[str]:
    return {
        path.relative_to(repo).as_posix()
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }


def tags(repo: Path) -> dict[str, str]:
    """`git ls-files -t` status per path: H materialized, S skip-worktree."""
    rows = git(repo, "ls-files", "-t").splitlines()
    return {row[2:]: row[0] for row in rows}
