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

import pytest


def _scrub_git_env_keys(env: dict[str, str]) -> None:
    """Remove inherited git overlay keys that steer subprocesses outside tmp_path repos."""
    for key in list(env):
        if (
            key == "GIT_CONFIG_COUNT"
            or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
            or key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
        ):
            del env[key]


def scrubbed_git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Lane IDD runs may set core.hooksPath via GIT_CONFIG_*; test git calls must not inherit it."""
    env = dict(os.environ)
    _scrub_git_env_keys(env)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    if extra:
        env.update(extra)
    return env


def isolate_process_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the same overlay from the process so code paths that spawn git without env= stay hermetic."""
    for key in list(os.environ):
        if (
            key == "GIT_CONFIG_COUNT"
            or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
            or key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "githooks" / "post-checkout-sparse-worktree.sh"
MODULE = ROOT / "scripts" / "sparse_worktree.py"

# Media the patterns must leave out, media they must keep, and prose beside both.
EXCLUDED = (
    "(private path)",
    "(private path)",
    "(private path)",
    "(private path)",
)
KEPT = (
    "(private path)",
    "(private path)",
    "(private path)",
    "docs/assets/diagram.png",
    "apps/web/icon.png",
    "apps/web/module.py",
    "uv.lock",
    "scripts/__init__.py",
    "scripts/sparse_worktree.py",
)
# A CGNAT address inside an excluded image: the publication audit must still find it.
LEAK_PATH = "(private path)"
# Split so this file itself never carries the address (the audit scans tests too).
_CGNAT = "100." + "101.102.103"
LEAK_TEXT = f"render notes for {_CGNAT}\n".encode()
# Prose linking INTO the excluded media, plus one genuinely dangling link as the
# control that the link probe can still say "dangling" in both trees.
LINKING_DOC = "(private path)"
LINKING_TEXT = b"![shot](shot.jpeg) [deeper](nested/deeper.png) [gone](missing.png)\n"
CONTENT = {LEAK_PATH: LEAK_TEXT, LINKING_DOC: LINKING_TEXT}


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=scrubbed_git_env(env),
    )
    assert done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr}"
    return done.stdout + done.stderr


def build_primary(tmp_path: Path) -> Path:
    """A primary checkout with the hook installed the way `wt-sparse-hook-install` does."""
    primary = tmp_path / "primary"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(primary)],
        check=True,
        env=scrubbed_git_env(),
    )
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
