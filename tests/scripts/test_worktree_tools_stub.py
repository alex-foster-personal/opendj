"""The caller-side stub for fleet-af's agent worktree tools (OPS-21, OPS-22, OPS-41).

The worktree registry, reaper, creation guard and worker `create` guard moved to
fleet-af (`agents_worktree_tools/`, Fri 2 Oct 2026) and their suite runs there.
What stays here is the contract this checkout owns: `just wt-*` and `just
worker-worktree` reach the fleet-af copy through `scripts/worktree_tools.py`,
which hands it this checkout as `--repo`, or exits 2 with the reason.

  - [if] a `wt-*` recipe runs [then] fleet-af's tool gets this checkout as --repo, [else stop].
  - [if] fleet-af's copy is absent [then] exit 2 naming the path, never a fallback, [else stop].

Nothing here is fabricated. The hand-off tests run the REAL fleet-af tools, found
the way the stub finds them ($FLEET_AF_HOME, else the live clone), against this
checkout and against throwaway git repositories. Where no fleet-af copy exists (CI
runners: fleet-af is private) those tests report UNAVAILABLE with the path they
looked at, never a pass. The argv rule itself is checked on the stub's own pure
`tool_argv`, and the refusals on a real empty directory, so both still bite there.

Regression one-liners:
  - if `lifecycle` is not handed `--repo <this checkout>` right after its subcommand then broken
  - if a `--repo` the caller passes does not come after (and so win over) the stub's then broken
  - if `worker_guard` args are rewritten in any way then broken
  - if the tool's exit code does not come back unchanged then broken
  - if a missing fleet-af copy, or an unknown tool, does not exit 2 with the reason then broken
  - if an unset FLEET_AF_HOME does not resolve to the live clone (never ~/code/fleet-af) then broken
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import worktree_tools

pytestmark = pytest.mark.requirement("OPS-21")

REPO_ROOT = Path(__file__).resolve().parents[2]
_STATUS = ("status", "--no-github", "--no-size", "--no-remote", "--json")


def _real_fleet_af() -> Path:
    """The fleet-af copy the stub would run, or an UNAVAILABLE report naming where it looked."""
    home = worktree_tools.fleet_af_home()
    missing = [t for t in worktree_tools.TOOLS
               if not (home / worktree_tools.PACKAGE / f"{t}.py").is_file()]
    if missing:
        pytest.skip(f"UNAVAILABLE: no fleet-af {worktree_tools.PACKAGE} {missing} under {home} "
                    "(set FLEET_AF_HOME or create the live clone); the hand-off was not measured")
    return home


def _run(fleet_af: Path, *args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "FLEET_AF_HOME": str(fleet_af), "PYTHONPATH": str(REPO_ROOT)}
    return subprocess.run(
        [sys.executable, "-m", "scripts.worktree_tools", *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=300, check=False,
    )


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    for cmd in (["init", "-q", "-b", "main"],
                ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
                 "commit", "-q", "--allow-empty", "-m", "root"]):
        subprocess.run(["git", *cmd], cwd=path, check=True, capture_output=True)
    return path


def _listed(done: subprocess.CompletedProcess[str]) -> set[Path]:
    assert done.returncode == 0, done.stderr
    return {Path(row["path"]).resolve() for row in json.loads(done.stdout)}


def test_lifecycle_argv_puts_this_checkout_right_after_the_subcommand() -> None:
    assert worktree_tools.tool_argv("lifecycle", ["guard", "--cap", "3"]) == [
        "guard", "--repo", str(REPO_ROOT), "--cap", "3"]
    # A caller's --repo follows the stub's, so argparse's last-wins gives it the say.
    assert worktree_tools.tool_argv("lifecycle", ["status", "--repo", "/elsewhere"]) == [
        "status", "--repo", str(REPO_ROOT), "--repo", "/elsewhere"]


def test_worker_guard_argv_is_untouched() -> None:
    """Control for the --repo injection: only `lifecycle` gets it."""
    argv = ["create", "--repo", "/r", "--target", "/t", "--branch", "b", "--base", "origin/main"]
    assert worktree_tools.tool_argv("worker_guard", argv) == argv


def test_real_lifecycle_lists_this_checkout_from_anywhere(tmp_path: Path) -> None:
    """[if] a `wt-*` recipe runs [then] the real tool gets this checkout as --repo, [else stop].

    Run from an unrelated directory: the tool requires --repo, so a listing that
    contains this checkout can only come from the stub's hand-off."""
    listed = _listed(_run(_real_fleet_af(), "lifecycle", *_STATUS, cwd=tmp_path))
    assert REPO_ROOT.resolve() in listed


def test_real_lifecycle_honors_a_callers_repo(tmp_path: Path) -> None:
    """[if] the caller names --repo [then] the real tool scans that repository instead."""
    other = _git_repo(tmp_path / "other")
    listed = _listed(_run(_real_fleet_af(), "lifecycle", *_STATUS, "--repo", str(other)))
    assert listed == {other.resolve()}


def test_real_worker_guard_refusal_comes_back_unchanged(tmp_path: Path) -> None:
    """[if] the real guard refuses [then] its exit code and reason reach the caller as-is.

    A repository with no `origin` has no remote-tracking base, so the fail-closed
    guard must refuse and create nothing."""
    repo, target = _git_repo(tmp_path / "repo"), tmp_path / "target"
    done = _run(_real_fleet_af(), "worker_guard", "create", "--repo", str(repo),
                "--target", str(target), "--branch", "af--x", "--base", "origin/main")
    assert done.returncode != 0 and done.returncode != worktree_tools.EXIT_UNAVAILABLE
    assert "origin/main" in done.stderr and not target.exists()


def test_missing_fleet_af_copy_exits_2_naming_the_path(tmp_path: Path) -> None:
    """[if] fleet-af has no such tool [then] exit 2 naming the path and FLEET_AF_HOME."""
    done = _run(tmp_path, "lifecycle", "guard")
    assert done.returncode == 2
    assert str(tmp_path / "agents_worktree_tools" / "lifecycle.py") in done.stderr
    assert "FLEET_AF_HOME" in done.stderr and done.stdout == ""


def test_unknown_tool_exits_2(tmp_path: Path) -> None:
    done = _run(tmp_path, "census", "status")
    assert done.returncode == 2 and "usage:" in done.stderr


def test_unset_home_resolves_to_the_live_clone(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    """[if] FLEET_AF_HOME is unset [then] the live clone, never ~/code/fleet-af, [else stop]."""
    monkeypatch.delenv("FLEET_AF_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert worktree_tools.fleet_af_home() == tmp_path / ".local" / "share" / "fleet-af-live"
    monkeypatch.setenv("FLEET_AF_HOME", str(tmp_path / "x"))
    assert worktree_tools.fleet_af_home() == tmp_path / "x"
