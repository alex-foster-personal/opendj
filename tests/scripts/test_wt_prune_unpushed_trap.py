"""Regression coverage for afmac/ci-lane/wt-prune.sh's merge criterion.

Prior incident (see CLAUDE.md, "KNOWN PRIOR BUG"): an earlier version of this
launchd-scheduled pruner treated "no origin ref for this branch" as "merged",
and deleted a worktree holding a branch that had simply never been pushed.

The current script instead asks GitHub directly (`gh pr list` / `gh pr view`)
whether a MERGED pull request with a green CI rollup exists for the branch,
and never infers merge state from the mere absence of a remote-tracking ref.
These tests drive the real script against throwaway git repos with a stubbed
`gh` on PATH, so the thing under test is the script's own decision logic, not
a reimplementation of it.

  - [if] a branch has commits with no matching origin ref and gh reports no
    merged PR for it [then] the worktree and branch survive, [else broken].
  - [if] gh reports a merged PR with an all-green statusCheckRollup for the
    branch [then] the worktree is removed and the branch is deleted, [else
    broken] (a positive control: proves the mechanism still fires at all).
  - [if] `gh pr list` fails (auth broken, rate limited, malformed reply)
    [then] the worktree is reported UNKNOWN and kept, never guessed as
    merged or unmerged, [else broken].
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

WT_PRUNE_SH = Path.home() / "code/afmac/ci-lane/wt-prune.sh"

pytestmark = pytest.mark.skipif(
    not WT_PRUNE_SH.is_file(), reason=f"{WT_PRUNE_SH} not present on this machine"
)


# -----------------------------------------------------------------------------
# _helpers
# -----------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return done.stdout


def _make_origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(origin)], check=True)

    clone = tmp_path / "lanes"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)], check=True)
    (clone / "README.md").write_text("root\n")
    _git(clone, "add", "README.md")
    _git(clone, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "root")
    _git(clone, "push", "-q", "-u", "origin", "main")
    return origin, clone


def _add_worktree_with_local_commit(clone: Path, branch: str, wt_dir: Path) -> None:
    """A worktree on a NEW branch holding one commit that is pushed nowhere."""
    _git(clone, "worktree", "add", "-q", "-b", branch, str(wt_dir), "main")
    (wt_dir / "work.txt").write_text("in progress\n")
    _git(wt_dir, "add", "work.txt")
    _git(wt_dir, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "wip")


def _fake_gh(bin_dir: Path, *, auth_ok: bool, pr_list_json: str | None, pr_view_json: str | None) -> None:
    """A stand-in for `gh` that answers exactly the calls this script makes."""
    script = bin_dir / "gh"
    lines = ["#!/bin/bash", "set -u"]
    if auth_ok:
        lines += ['if [ "$1" = "auth" ] && [ "$2" = "status" ]; then exit 0; fi']
    else:
        lines += ['if [ "$1" = "auth" ] && [ "$2" = "status" ]; then exit 1; fi']
    if pr_list_json is not None:
        lines += [
            'if [ "$1" = "pr" ] && [ "$2" = "list" ]; then',
            f"  cat <<'PRLIST'\n{pr_list_json}\nPRLIST",
            "  exit 0",
            "fi",
        ]
    else:
        lines += ['if [ "$1" = "pr" ] && [ "$2" = "list" ]; then echo "fake_gh: pr list unavailable" >&2; exit 1; fi']
    if pr_view_json is not None:
        lines += [
            'if [ "$1" = "pr" ] && [ "$2" = "view" ]; then',
            f"  cat <<'PRVIEW'\n{pr_view_json}\nPRVIEW",
            "  exit 0",
            "fi",
        ]
    lines += ['echo "fake_gh: unhandled args: $*" >&2', "exit 1"]
    script.write_text("\n".join(lines) + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _run_prune(clone: Path, bin_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["WT_PRUNE_ROOTS"] = str(clone)
    env["WT_PRUNE_GH_REPO"] = "example-org/example-repo"
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(WT_PRUNE_SH)], env=env, capture_output=True, text=True, timeout=30
    )


# -----------------------------------------------------------------------------
# tests
# -----------------------------------------------------------------------------


def test_unpushed_branch_survives_prune(tmp_path: Path) -> None:
    _origin, clone = _make_origin_and_clone(tmp_path)
    wt_dir = tmp_path / "wt-never-pushed"
    branch = "af--never-pushed"
    _add_worktree_with_local_commit(clone, branch, wt_dir)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_gh(bin_dir, auth_ok=True, pr_list_json="[]", pr_view_json=None)

    result = _run_prune(clone, bin_dir)

    assert wt_dir.is_dir(), "unpushed-branch trap: worktree was deleted though gh found no merged PR for it"
    assert branch in _git(clone, "branch", "--list", branch), "unpushed-branch trap: branch was deleted though it was never merged"
    assert "KEEP-NOT-MERGED" in result.stdout, f"expected a KEEP-NOT-MERGED verdict, got: {result.stdout!r}"


def test_merged_and_green_branch_is_pruned(tmp_path: Path) -> None:
    _origin, clone = _make_origin_and_clone(tmp_path)
    wt_dir = tmp_path / "wt-merged-clean"
    branch = "af--merged-clean"
    _add_worktree_with_local_commit(clone, branch, wt_dir)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_gh(
        bin_dir,
        auth_ok=True,
        pr_list_json=json.dumps([{"number": 42, "state": "MERGED"}]),
        pr_view_json=json.dumps({"statusCheckRollup": [{"conclusion": "SUCCESS"}]}),
    )

    result = _run_prune(clone, bin_dir)

    assert not wt_dir.exists(), f"positive control failed: merged+CI-green worktree was not removed. stdout: {result.stdout!r}"
    assert branch not in _git(clone, "branch", "--list", branch), "positive control failed: branch was not deleted after removal"
    assert "REMOVED" in result.stdout, f"expected a REMOVED verdict, got: {result.stdout!r}"


def test_gh_failure_reports_unknown_and_keeps(tmp_path: Path) -> None:
    _origin, clone = _make_origin_and_clone(tmp_path)
    wt_dir = tmp_path / "wt-gh-down"
    branch = "af--gh-down"
    _add_worktree_with_local_commit(clone, branch, wt_dir)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_gh(bin_dir, auth_ok=True, pr_list_json=None, pr_view_json=None)

    result = _run_prune(clone, bin_dir)

    assert wt_dir.is_dir(), "verification.md violation: a failed gh call must never be read as a verdict to remove"
    assert "UNKNOWN" in result.stdout, f"expected an UNKNOWN verdict on gh failure, got: {result.stdout!r}"
