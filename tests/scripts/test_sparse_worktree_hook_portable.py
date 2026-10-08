"""The installed sparse-worktree hook is portable and fails closed (OPS-45, #5212, #5995).

[if] the hook's Python is missing [then] it exits nonzero naming the error, [else stop].

Real repositories, the real hook script run by real bash and by real
`git worktree add`. "Missing" is a real PATH that holds only `git` and `bash`
(links to the real programs), so no Python is reachable; nothing is stubbed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import sparse_worktree as sw
from tests.scripts.sparse_worktree_fixtures import (
    EXCLUDED,
    add_worktree,
    build_primary,
    git,
    on_disk,
    tags,
)

pytestmark = pytest.mark.requirement("OPS-45")

NULL = "0" * 40
NAMED_ERROR = "SPARSE_HOOK_PYTHON_UNAVAILABLE"
HOOK_EXIT_UNAVAILABLE = 3


def _resolved(program: str) -> str:
    found = shutil.which(program)
    if found is None:
        pytest.fail(f"UNAVAILABLE: {program} is not on PATH, so the hook cannot be run at all")
    return found


def _bin_dir(tmp_path: Path, links: dict[str, str]) -> Path:
    """A directory holding ONLY the named programs, as links to the real ones."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    suffix = ".exe" if os.name == "nt" else ""
    for name, target in links.items():
        try:
            (bin_dir / f"{name}{suffix}").symlink_to(target)
        except OSError as exc:  # pragma: no cover - Windows without symlink privilege
            pytest.skip(f"creating a symlink needs Windows Developer Mode or admin rights: {exc}")
    return bin_dir


def _env(bin_dir: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("MDT_SPARSE_HOOK_PYTHON", sw.CFG.OPT_OUT_ENV)}
    env["PATH"] = str(bin_dir)
    env.update(extra)
    return env


def _run_hook(hook: Path, worktree: Path, env: dict[str, str], previous: str = NULL) -> subprocess.CompletedProcess[str]:
    head = git(worktree, "rev-parse", "HEAD").strip()
    return subprocess.run(
        [_resolved("bash"), str(hook), previous, head, "1"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.fixture
def primary(tmp_path: Path) -> Path:
    return build_primary(tmp_path)


def test_hook_with_no_python_exits_nonzero_with_named_error(primary: Path, tmp_path: Path) -> None:
    """[if] no Python 3 is reachable [then] the hook exits 3 naming the error, [else stop]."""
    worktree = tmp_path / "wt-no-python"
    add_worktree(primary, worktree, env={sw.CFG.OPT_OUT_ENV: "1"})
    bin_dir = _bin_dir(tmp_path, {"git": _resolved("git"), "bash": _resolved("bash")})
    done = _run_hook(tmp_path / "hooks" / "post-checkout", worktree, _env(bin_dir))
    print(f"hook rc={done.returncode} stderr={done.stderr.strip()}")
    assert done.returncode == HOOK_EXIT_UNAVAILABLE, (done.returncode, done.stderr)
    assert f"[ERROR] sparse-worktree: {NAMED_ERROR}" in done.stderr, done.stderr
    assert "tried: python3 python py -3" in done.stderr, done.stderr
    assert not sw.is_sparse(worktree), "the error must describe the real state: not sparse"


def test_worktree_add_with_no_python_reports_the_named_error(primary: Path, tmp_path: Path) -> None:
    """[if] git worktree add runs the hook with no Python [then] the add fails loud, [else stop]."""
    git_path = _resolved("git")
    bin_dir = _bin_dir(tmp_path, {"git": git_path, "bash": _resolved("bash")})
    worktree = tmp_path / "wt-add-no-python"
    done = subprocess.run(
        [git_path, "-C", str(primary), "worktree", "add", "-q", str(worktree), "-b", worktree.name],
        capture_output=True,
        text=True,
        check=False,
        env=_env(bin_dir),
    )
    print(f"git worktree add rc={done.returncode} stderr={done.stderr.strip()}")
    assert done.returncode != 0, (done.returncode, done.stderr)
    assert NAMED_ERROR in done.stderr, done.stderr
    assert set(EXCLUDED) <= on_disk(worktree), "the worktree exists and is full, as reported"


def test_named_interpreter_that_is_missing_is_not_replaced(primary: Path, tmp_path: Path) -> None:
    """[if] MDT_SPARSE_HOOK_PYTHON names a missing program [then] exit 3, no fallback, [else stop]."""
    worktree = tmp_path / "wt-bad-override"
    add_worktree(primary, worktree, env={sw.CFG.OPT_OUT_ENV: "1"})
    bin_dir = _bin_dir(
        tmp_path, {"git": _resolved("git"), "bash": _resolved("bash"), "python3": sys.executable}
    )
    missing = str(tmp_path / "no-such-python")
    done = _run_hook(
        tmp_path / "hooks" / "post-checkout", worktree, _env(bin_dir, MDT_SPARSE_HOOK_PYTHON=missing)
    )
    assert done.returncode == HOOK_EXIT_UNAVAILABLE, (done.returncode, done.stderr)
    assert NAMED_ERROR in done.stderr and missing in done.stderr, done.stderr


def test_plain_python_is_used_when_python3_is_absent(primary: Path, tmp_path: Path) -> None:
    """[if] only `python` exists, as on native Windows [then] the new worktree is sparse, [else stop]."""
    git_path = _resolved("git")
    bin_dir = _bin_dir(
        tmp_path, {"git": git_path, "bash": _resolved("bash"), "python": sys.executable}
    )
    worktree = tmp_path / "wt-plain-python"
    done = subprocess.run(
        [git_path, "-C", str(primary), "worktree", "add", "-q", str(worktree), "-b", worktree.name],
        capture_output=True,
        text=True,
        check=False,
        env=_env(bin_dir),
    )
    assert done.returncode == 0, done.stderr
    assert sw.is_sparse(worktree), done.stderr
    assert {tags(worktree)[rel] for rel in EXCLUDED} == {"S"}
    assert not set(EXCLUDED) & on_disk(worktree)


def test_branch_switch_needs_no_interpreter(primary: Path, tmp_path: Path) -> None:
    """[if] the checkout is a branch switch and no Python exists [then] exit 0, [else stop]."""
    worktree = tmp_path / "wt-switch"
    add_worktree(primary, worktree)
    bin_dir = _bin_dir(tmp_path, {"git": _resolved("git"), "bash": _resolved("bash")})
    head = git(worktree, "rev-parse", "HEAD").strip()
    done = _run_hook(tmp_path / "hooks" / "post-checkout", worktree, _env(bin_dir), previous=head)
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""


def test_install_hook_upgrades_an_older_copy_of_itself(tmp_path: Path) -> None:
    """[if] install-hook meets an older copy of this hook [then] it is upgraded, [else stop]."""
    repo = tmp_path / "repo"
    subprocess.run([_resolved("git"), "init", "-q", str(repo)], check=True)
    installed = sw.install_hook(repo)
    older = (
        "#!/usr/bin/env bash\n"
        "command -v python3 >/dev/null 2>&1 || exit 0\n"
        'python3 -m scripts.sparse_worktree post-checkout "$1" "$2" "$3"\n'
    )
    installed.write_text(older, encoding="utf-8", newline="\n")
    assert sw.install_hook(repo) == installed
    assert installed.read_bytes() == sw.CFG.HOOK_SOURCE.read_bytes()
