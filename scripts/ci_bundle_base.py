"""Resolve the PR bundle-budget baseline SHA as the merge commit's first parent.

GitHub Actions' pull_request event ``base.sha`` is fixed when the event fires and
can trail the merge ref's main parent by many commits (#4985). This helper reads
HEAD of the merge checkout, requires a second parent, and writes ``sha=<40-hex>``
to ``$GITHUB_OUTPUT`` so later steps fetch and measure against ``HEAD^1``. Usage:
``python3 -m scripts.ci_bundle_base``. Required env: ``EVENT_BASE_SHA``,
``GITHUB_OUTPUT``. Stdlib + ``scripts.ci_path_resolve``.
"""

from __future__ import annotations

import functools
import os
import subprocess
from pathlib import Path

from scripts.ci_path_resolve import require_on_path


class BundleBaseError(Exception):
    """HEAD is not a 2-parent merge, git is missing, or required env is missing."""


@functools.cache
def _git_exe() -> str:
    """git by resolved path, once (AGENTS.md: launch programs by resolved path)."""
    return require_on_path(
        "git",
        BundleBaseError,
        "git is not on PATH: cannot resolve the bundle baseline",
    )


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [_git_exe(), *args],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=check,
        )
    except subprocess.CalledProcessError as exc:
        raise BundleBaseError(f"git {' '.join(args)} failed: {(exc.stderr or '').strip()}") from exc


def resolve_merge_parent(repo: Path) -> str:
    """First parent of the PR merge commit at repo HEAD. Raises BundleBaseError otherwise."""
    second = _git(repo, "rev-parse", "--verify", "--quiet", "HEAD^2", check=False)
    if second.returncode != 0:
        short = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
        raise BundleBaseError(
            f"HEAD {short} is not the PR merge commit, so it has no main parent to measure against"
        )
    sha = _git(repo, "rev-parse", "HEAD^1").stdout.strip()
    if not sha:
        raise BundleBaseError("HEAD^1 did not resolve to a SHA")
    return sha


def _annotation(kind: str, body: str) -> str:
    return f"::{kind} title=Bundle budgets::{body}"


def main() -> int:
    """Read EVENT_BASE_SHA + GITHUB_OUTPUT, cwd = merge checkout, write sha=."""
    event_base_sha = os.environ.get("EVENT_BASE_SHA", "")
    output = os.environ.get("GITHUB_OUTPUT", "")
    if not event_base_sha:
        print(_annotation("error", "EVENT_BASE_SHA must be set"))
        return 1
    if not output:
        print(_annotation("error", "GITHUB_OUTPUT must be set"))
        return 1
    try:
        sha = resolve_merge_parent(Path.cwd())
    except BundleBaseError as exc:
        print(_annotation("error", str(exc)))
        return 1
    if sha != event_base_sha:
        print(
            _annotation(
                "notice",
                f"event base.sha {event_base_sha[:10]} is not the merge parent; "
                f"measuring against {sha[:10]}, the main commit this merge was built on",
            )
        )
    with Path(output).open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"sha={sha}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
