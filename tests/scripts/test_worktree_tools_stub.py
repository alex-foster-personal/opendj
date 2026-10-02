"""The caller-side stub for fleet-af's agent worktree tools (OPS-21, OPS-22, OPS-41).

The worktree registry, reaper, creation guard and worker `create` guard moved to
fleet-af (`agents_worktree_tools/`, Fri 2 Oct 2026) and their suite runs there.
What stays here is the contract this checkout owns: `just wt-*` and `just
worker-worktree` reach the fleet-af copy through `scripts/worktree_tools.py`,
which hands it this checkout as `--repo`, or exits 2 with the reason.

  - [if] a `wt-*` recipe runs [then] fleet-af's tool gets this checkout as --repo, [else stop].
  - [if] fleet-af's copy is absent [then] exit 2 naming the path, never a fallback, [else stop].

The tool under FLEET_AF_HOME is a temp module that records the argv it was
handed: CI runners have no fleet-af checkout, and the stub's job ends at the
hand-off. The real tools' tests are fleet-af's `just worktree-tools::test`.

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

_RECORDER = """import json, sys
print(json.dumps(sys.argv[1:]))
raise SystemExit(7)
"""


def _fleet_af(tmp_path: Path, *tools: str) -> Path:
    package = tmp_path / "fleet-af" / "agents_worktree_tools"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for tool in tools:
        (package / f"{tool}.py").write_text(_RECORDER, encoding="utf-8")
    return package.parent


def _run(fleet_af: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "FLEET_AF_HOME": str(fleet_af)}
    return subprocess.run(
        [sys.executable, "-m", "scripts.worktree_tools", *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60, check=False,
    )


def test_lifecycle_is_handed_this_checkout_as_repo(tmp_path: Path) -> None:
    """[if] a `wt-*` recipe runs [then] the tool gets this checkout as --repo, [else stop]."""
    done = _run(_fleet_af(tmp_path, "lifecycle"), "lifecycle", "guard", "--cap", "3")
    assert done.returncode == 7, done.stderr
    assert json.loads(done.stdout) == ["guard", "--repo", str(REPO_ROOT), "--cap", "3"]


def test_a_callers_repo_comes_last_and_so_wins(tmp_path: Path) -> None:
    """[if] the caller names --repo [then] it follows the stub's (argparse: last wins)."""
    done = _run(_fleet_af(tmp_path, "lifecycle"), "lifecycle", "status", "--repo", "/elsewhere")
    assert json.loads(done.stdout) == ["status", "--repo", str(REPO_ROOT), "--repo", "/elsewhere"]


def test_worker_guard_args_pass_through_untouched(tmp_path: Path) -> None:
    """Control for the --repo injection: only `lifecycle` gets it."""
    argv = ["create", "--repo", "/r", "--target", "/t", "--branch", "b", "--base", "origin/main"]
    done = _run(_fleet_af(tmp_path, "worker_guard"), "worker_guard", *argv)
    assert done.returncode == 7, done.stderr
    assert json.loads(done.stdout) == argv


def test_missing_fleet_af_copy_exits_2_naming_the_path(tmp_path: Path) -> None:
    """[if] fleet-af has no such tool [then] exit 2 naming the path and FLEET_AF_HOME."""
    home = _fleet_af(tmp_path, "worker_guard")   # the package exists, lifecycle does not
    done = _run(home, "lifecycle", "guard")
    assert done.returncode == 2
    assert str(home / "agents_worktree_tools" / "lifecycle.py") in done.stderr
    assert "FLEET_AF_HOME" in done.stderr and done.stdout == ""


def test_unknown_tool_exits_2(tmp_path: Path) -> None:
    done = _run(_fleet_af(tmp_path, "lifecycle"), "census", "status")
    assert done.returncode == 2 and "usage:" in done.stderr


def test_unset_home_resolves_to_the_live_clone(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    """[if] FLEET_AF_HOME is unset [then] the live clone, never ~/code/fleet-af, [else stop]."""
    monkeypatch.delenv("FLEET_AF_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert worktree_tools.fleet_af_home() == tmp_path / ".local" / "share" / "fleet-af-live"
    monkeypatch.setenv("FLEET_AF_HOME", str(tmp_path / "x"))
    assert worktree_tools.fleet_af_home() == tmp_path / "x"
