"""Hermetic tests for ops/fleet/moc-refresh.sh's per-run re-pin (issue #3715).

Fixture: a throwaway bare "origin" repo plus a worktree checked out to its
first commit, standing in for the live ~/jobs/lanes/docs/moc-refresh-repo
worktree that the weekly moc-refresh.timer re-pins to origin/main before it
runs scripts/moc_refresh.py. MOC_REFRESH_CMD stands in for that checker so
these tests never invoke real citation checking, gh, or network.

The shared-checkout test is the reason this file exists at all: the deployed
timer is a systemd unit on nucbox, so a bad default here does not fail a test
batch, it detaches a tree that live dispatch lanes read every few minutes.

Regression lines:
  - if a run with no explicit repo root operates on the shared
    $HOME/code/music-dj-tools checkout -- fetching into it, or detaching it off
    whatever branch a live lane is holding -- instead of refusing, then broken
  - if a tracked-file change in the runtime worktree is silently checked out
    over, instead of refusing the run, then broken
  - if an untracked stray (.venv symlink, __pycache__) blocks the re-pin
    then broken
  - if a fetch failure is silently swallowed instead of refusing the run
    then broken
  - if a checker's nonzero exit (citation drift) is reported as a unit success
    then broken
  - if the checker's own table is not appended to the log, so a reader cannot
    tell WHICH citations drifted, then broken
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOC_REFRESH = REPO / "ops" / "fleet" / "moc-refresh.sh"

STUB_CMD_OK = "true"
STUB_CMD_FAIL = "false"

# The live runtime worktree the default repo root must be pinned to; a bare
# deploy on nucbox has to land here, not on the shared checkout.
LIVE_RUNTIME_REPO = "jobs/lanes/docs/moc-refresh-repo"


def _commit_and_push(seed: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(seed), "add", "f.txt"], check=True)
    cfg = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "-C", str(seed), *cfg, "commit", "-q", "-m", message], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)
    return subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_origin(tmp_path: Path) -> tuple[Path, str, str]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(seed)], check=True)
    (seed / "f.txt").write_text("v1\n", encoding="utf-8")
    sha1 = _commit_and_push(seed, "c1")
    (seed / "f.txt").write_text("v2\n", encoding="utf-8")
    sha2 = _commit_and_push(seed, "c2")
    return origin, sha1, sha2


def _stale_worktree(tmp_path: Path, origin: Path, pin_sha: str) -> Path:
    wt = tmp_path / "stale-worktree"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(wt)], check=True)
    subprocess.run(["git", "-C", str(wt), "checkout", "--quiet", "--detach", pin_sha], check=True)
    return wt


def _head(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _run(
    *,
    jobs_dir: Path,
    repo_root: Path | None = None,
    home: Path | None = None,
    cmd: str = STUB_CMD_OK,
    ref: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "JOBS_DIR": str(jobs_dir),
        "MOC_REFRESH_LOG": str(jobs_dir / "moc-refresh.log"),
        "MOC_REFRESH_CMD": cmd,
    }
    # A stray value in the caller's environment would make the shared-checkout
    # test ask an easier question than the one it claims to ask, so the absence
    # of the override is asserted rather than assumed.
    env.pop("MOC_REFRESH_REPO_ROOT", None)
    if repo_root is not None:
        env["MOC_REFRESH_REPO_ROOT"] = str(repo_root)
    if home is not None:
        env["HOME"] = str(home)
    if ref is not None:
        env["MOC_REFRESH_REF"] = ref
    return subprocess.run(
        ["bash", str(MOC_REFRESH)], capture_output=True, text=True, env=env, check=False
    )


def _log(jobs_dir: Path) -> str:
    return (jobs_dir / "moc-refresh.log").read_text(encoding="utf-8")


def test_default_repo_root_never_operates_on_the_shared_checkout(tmp_path: Path) -> None:
    """[if] run with no repo-root override [then] the shared checkout's HEAD is never moved."""
    origin, sha1, sha2 = _init_origin(tmp_path)
    home = tmp_path / "home"
    shared = home / "code" / "music-dj-tools"
    shared.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "--quiet", str(origin), str(shared)], check=True)
    subprocess.run(["git", "-C", str(shared), "checkout", "--quiet", "--detach", sha1], check=True)
    assert _head(shared) == sha1, "fixture must leave the shared checkout on the stale pin"
    jobs_dir = tmp_path / "jobs"

    result = _run(jobs_dir=jobs_dir, home=home)

    assert _head(shared) == sha1, (
        "the shared checkout was re-pinned off its branch by a run that was given no repo root; "
        f"origin/main is {sha2}"
    )
    assert result.returncode != 0
    assert LIVE_RUNTIME_REPO in _log(jobs_dir)


def test_stale_worktree_is_repinned_to_origin_main_and_logged(tmp_path: Path) -> None:
    """[if] worktree stale at c1 and origin/main is at c2 [then] repinned to c2, logged."""
    origin, sha1, sha2 = _init_origin(tmp_path)
    wt = _stale_worktree(tmp_path, origin, sha1)
    jobs_dir = tmp_path / "jobs"

    result = _run(repo_root=wt, jobs_dir=jobs_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _head(wt) == sha2
    assert f"pinned ref=origin/main sha={sha2}" in _log(jobs_dir)


def test_dirty_tracked_file_refuses_without_touching_worktree(tmp_path: Path) -> None:
    """[if] worktree has a tracked-file edit [then] refuses, logs why, leaves HEAD alone."""
    origin, sha1, _sha2 = _init_origin(tmp_path)
    wt = _stale_worktree(tmp_path, origin, sha1)
    (wt / "f.txt").write_text("dirty\n", encoding="utf-8")
    jobs_dir = tmp_path / "jobs"

    result = _run(repo_root=wt, jobs_dir=jobs_dir)

    assert result.returncode != 0
    assert _head(wt) == sha1
    log = _log(jobs_dir)
    assert "ERROR refusing" in log
    assert "tracked changes" in log


def test_untracked_stray_does_not_block_repin(tmp_path: Path) -> None:
    """[if] worktree carries an untracked stray file (.venv-like) [then] repin still runs."""
    origin, sha1, sha2 = _init_origin(tmp_path)
    wt = _stale_worktree(tmp_path, origin, sha1)
    (wt / "stray-untracked.txt").write_text("not tracked\n", encoding="utf-8")
    jobs_dir = tmp_path / "jobs"

    result = _run(repo_root=wt, jobs_dir=jobs_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _head(wt) == sha2


def test_fetch_failure_refuses_and_leaves_worktree_alone(tmp_path: Path) -> None:
    """[if] origin remote is unreachable [then] refuses, logs why, leaves HEAD alone."""
    origin, sha1, _sha2 = _init_origin(tmp_path)
    wt = _stale_worktree(tmp_path, origin, sha1)
    subprocess.run(
        ["git", "-C", str(wt), "remote", "set-url", "origin", str(tmp_path / "no-such.git")],
        check=True,
    )
    jobs_dir = tmp_path / "jobs"

    result = _run(repo_root=wt, jobs_dir=jobs_dir)

    assert result.returncode != 0
    assert _head(wt) == sha1
    log = _log(jobs_dir)
    assert "ERROR" in log
    assert "fetch" in log


def test_missing_repo_root_refuses_without_running_the_checker(tmp_path: Path) -> None:
    """[if] the configured repo root does not exist [then] refuses before any checkout or check."""
    jobs_dir = tmp_path / "jobs"

    result = _run(repo_root=tmp_path / "absent-repo", jobs_dir=jobs_dir)

    assert result.returncode != 0
    log = _log(jobs_dir)
    assert "ERROR missing repo root" in log
    assert "pinned ref=" not in log


def test_citation_drift_is_reported_as_a_nonzero_unit_result(tmp_path: Path) -> None:
    """[if] the checker exits nonzero (drift) [then] the unit exits nonzero and logs the sha."""
    origin, sha1, sha2 = _init_origin(tmp_path)
    wt = _stale_worktree(tmp_path, origin, sha1)
    jobs_dir = tmp_path / "jobs"

    result = _run(repo_root=wt, jobs_dir=jobs_dir, cmd=STUB_CMD_FAIL)

    assert result.returncode != 0
    log = _log(jobs_dir)
    assert f"drift or error rc=1 sha={sha2}" in log


def test_checker_table_is_appended_to_the_log(tmp_path: Path) -> None:
    """[if] the checker prints a table [then] its rows land in the log, not just the exit code."""
    origin, sha1, _sha2 = _init_origin(tmp_path)
    wt = _stale_worktree(tmp_path, origin, sha1)
    jobs_dir = tmp_path / "jobs"
    stub = "echo 'README.md path `docs/moc/gone.md` MISSING docs/moc/gone.md does not exist'; false"

    result = _run(repo_root=wt, jobs_dir=jobs_dir, cmd=stub)

    assert result.returncode != 0
    assert "`docs/moc/gone.md`" in _log(jobs_dir)


@pytest.fixture(scope="module", autouse=True)
def _require_moc_refresh_script() -> None:
    assert MOC_REFRESH.is_file(), f"missing {MOC_REFRESH}"
