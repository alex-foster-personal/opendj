"""Shared git-repo harness for the preview drift check suite.

Split out of ``test_preview_drift_check.py`` so that module and its
containment-focused sibling ``test_preview_drift_check_containment.py`` can
both build the same throwaway repo without duplicating the harness (and
without either file crossing the 600-line file-size ratchet).
"""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _git_env(cwd: Path, when: datetime | None = None) -> dict[str, str]:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(cwd),
    }
    if when is not None:
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S+0000")
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
    return env


def _run(cwd: Path, *args: str, when: datetime | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=_git_env(cwd, when), capture_output=True, text=True, check=True
    )
    return proc.stdout


def _commit(repo: Path, name: str, when: datetime) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _run(repo, "add", name, when=when)
    _run(repo, "commit", "-m", name, when=when)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repo with `main` and `preview`, both at the same commit."""
    r = tmp_path / "repo"
    r.mkdir()
    _run(r, "init", "-q", "-b", "main")
    base = datetime.now(UTC) - timedelta(days=10)
    _commit(r, "base.txt", base)
    _run(r, "branch", "preview")
    return r


def _hours_ago(n: float) -> datetime:
    return datetime.now(UTC) - timedelta(hours=n)
