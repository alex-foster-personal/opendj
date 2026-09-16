"""Regression coverage for afmac/ci-lane/artifact_cap_prune.py's safety guards.

The FILO build-artifact cap pruner deletes real directories (.venv,
node_modules, target, __pycache__) to keep disk usage under a cap. Per the
task that built it, it must NEVER touch a directory belonging to a worktree
that is dirty or whose branch is not verified merged-and-green on GitHub.
These tests drive the real script (not a reimplementation) against
throwaway git worktrees with a stubbed `gh`, so a regression in the actual
eviction logic turns these red.

  - [if] a worktree has uncommitted changes [then] its .venv survives
    eviction even while over cap, [else broken].
  - [if] a worktree's branch has no merged PR [then] its node_modules
    survives eviction even while over cap, [else broken].
  - [if] a worktree's branch is merged with an all-green CI rollup [then]
    its .venv IS evicted while over cap, [else broken] (positive control).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

CAP_PRUNE_PY = Path.home() / "code/afmac/ci-lane/artifact_cap_prune.py"

pytestmark = pytest.mark.skipif(
    not CAP_PRUNE_PY.is_file(), reason=f"{CAP_PRUNE_PY} not present on this machine"
)


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return done.stdout


def _make_worktree(tmp_path: Path, branch: str, *, dirty: bool) -> Path:
    origin = tmp_path / f"{branch}-origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(origin)], check=True)
    src = tmp_path / f"{branch}-src"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(src)], check=True)
    (src / "README.md").write_text("root\n")
    (src / ".gitignore").write_text(".venv/\nnode_modules/\n__pycache__/\n")
    _git(src, "add", "README.md", ".gitignore")
    _git(src, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "root")
    _git(src, "push", "-q", "-u", "origin", "main")

    wt = tmp_path / branch
    _git(src, "worktree", "add", "-q", "-b", branch, str(wt), "main")
    (wt / ".venv").mkdir()
    (wt / ".venv" / "payload.bin").write_bytes(b"x" * 4096)
    if dirty:
        (wt / "scratch.txt").write_text("uncommitted\n")
    return wt


def _fake_gh(bin_dir: Path, *, pr_list_json: str, pr_view_json: str | None) -> None:
    script = bin_dir / "gh"
    lines = [
        "#!/bin/bash",
        "set -u",
        'if [ "$1" = "pr" ] && [ "$2" = "list" ]; then',
        f"  cat <<'PRLIST'\n{pr_list_json}\nPRLIST",
        "  exit 0",
        "fi",
    ]
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


def _run_cap_prune(
    tmp_path: Path, bin_dir: Path, *, project_filter: str = ""
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["WT_CAP_SCAN_ROOTS"] = str(tmp_path)
    env["WT_CAP_BYTES"] = "0"  # force every candidate to be considered
    env["WT_PRUNE_GH_REPO"] = "example-org/example-repo"
    env["WT_CAP_PROJECT_FILTER"] = project_filter  # "" disables the guard for these fixtures
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return subprocess.run(
        ["python3", str(CAP_PRUNE_PY)], env=env, capture_output=True, text=True, timeout=30
    )


def test_dirty_worktree_venv_survives(tmp_path: Path) -> None:
    wt = _make_worktree(tmp_path, "af--dirty-example", dirty=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_gh(bin_dir, pr_list_json="[]", pr_view_json=None)

    result = _run_cap_prune(tmp_path, bin_dir)

    assert (wt / ".venv").is_dir(), f"dirty worktree's .venv was evicted. stdout: {result.stdout!r}"
    assert "DIRTY" in result.stdout, f"expected a DIRTY-guard KEEP, got: {result.stdout!r}"


def test_unmerged_branch_node_modules_survives(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(origin)], check=True)
    src = tmp_path / "src"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(src)], check=True)
    (src / "README.md").write_text("root\n")
    (src / ".gitignore").write_text(".venv/\nnode_modules/\n__pycache__/\n")
    _git(src, "add", "README.md", ".gitignore")
    _git(src, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "root")
    _git(src, "push", "-q", "-u", "origin", "main")
    wt = tmp_path / "af--unmerged-example"
    _git(src, "worktree", "add", "-q", "-b", "af--unmerged-example", str(wt), "main")
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "payload.bin").write_bytes(b"x" * 4096)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_gh(bin_dir, pr_list_json="[]", pr_view_json=None)

    result = _run_cap_prune(tmp_path, bin_dir)

    assert (wt / "node_modules").is_dir(), f"unmerged worktree's node_modules was evicted. stdout: {result.stdout!r}"
    assert "NOT_ELIGIBLE" in result.stdout, f"expected a NOT_ELIGIBLE KEEP, got: {result.stdout!r}"


def test_merged_and_green_worktree_venv_is_evicted(tmp_path: Path) -> None:
    wt = _make_worktree(tmp_path, "af--merged-example", dirty=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_gh(
        bin_dir,
        pr_list_json=json.dumps([{"number": 7, "state": "MERGED"}]),
        pr_view_json=json.dumps({"statusCheckRollup": [{"conclusion": "SUCCESS"}]}),
    )

    result = _run_cap_prune(tmp_path, bin_dir)

    assert not (wt / ".venv").exists(), f"positive control failed: merged+green .venv was not evicted. stdout: {result.stdout!r}"
    assert "EVICTED" in result.stdout, f"expected an EVICTED verdict, got: {result.stdout!r}"


def test_out_of_scope_project_survives_even_when_merged_and_green(tmp_path: Path) -> None:
    """Regression for the Wed 16 Sep 2026 incident: a wide SCAN_ROOTS (~/code)
    reached unrelated projects (orbital-af-ai-2, bruno-all-files/Investors-Hub,
    private-profile, tutorials/idd-software-factory) and evicted ~2 GiB of their
    node_modules/__pycache__ - none of which this pruner was ever meant to
    touch. The default WT_CAP_PROJECT_FILTER='music-dj-tools' must keep any
    path that does not contain it, no matter how eligible it otherwise looks.
    """
    other_project_root = tmp_path / "some-unrelated-client-project"
    other_project_root.mkdir()
    wt = _make_worktree(other_project_root, "af--merged-example", dirty=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_gh(
        bin_dir,
        pr_list_json=json.dumps([{"number": 7, "state": "MERGED"}]),
        pr_view_json=json.dumps({"statusCheckRollup": [{"conclusion": "SUCCESS"}]}),
    )

    result = _run_cap_prune(tmp_path, bin_dir, project_filter="music-dj-tools")

    assert (wt / ".venv").is_dir(), f"scope guard regression: unrelated project's .venv was evicted. stdout: {result.stdout!r}"
    assert "out-of-scope" in result.stdout, f"expected the scan-complete line to report an out-of-scope skip, got: {result.stdout!r}"
