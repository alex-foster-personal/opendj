"""Hermetic tests for ops/fleet/sink-triage.sh's per-run re-pin (issue #2896).

Fixture: a throwaway bare "origin" repo plus a worktree checked out to its
first commit, standing in for the live sink-triage-repo worktree that a
timer tick found stale at da2db8c29 while origin/main had already moved to
the OPS-34 fix. SINK_TRIAGE_CMD stands in for scripts.sink_triage so these
tests never invoke real triage logic, gh, or network.

Regression lines:
  - if the worktree is left at whatever commit it last happened to be on,
    instead of re-pinned to origin/main before every run, then broken
  - if a tracked-file change in the worktree is silently checked out over,
    instead of refusing the run, then broken
  - if an untracked stray (.venv symlink, __pycache__) blocks the re-pin
    then broken
  - if a fetch failure is silently swallowed instead of refusing the run
    then broken
  - if state/sink-triage-kpi.json does not end up carrying the sha the run
    actually executed at, so an audit cannot tell which code filed an
    issue, then broken
  - if a dry run or a failed triage run stamps a sha into the kpi file
    anyway, mislabeling a file scripts.sink_triage did not touch, then
    broken
  - if merging in sha/ref clobbers the fields scripts.sink_triage itself
    wrote (new_issues, fingerprints) then broken
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SINK_TRIAGE = REPO / "ops" / "fleet" / "sink-triage.sh"

STUB_CMD_OK = "true"
STUB_CMD_FAIL = "false"


def _commit_and_push(seed: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(seed), "add", "f.txt"], check=True)
    cfg = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(
        ["git", "-C", str(seed), *cfg, "commit", "-q", "-m", message],
        check=True,
    )
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


def _run(
    *,
    repo_root: Path,
    jobs_dir: Path,
    cmd: str = STUB_CMD_OK,
    ref: str | None = None,
    dry_run: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "SINK_TRIAGE_REPO_ROOT": str(repo_root),
        "JOBS_DIR": str(jobs_dir),
        "SINK_TRIAGE_LOG": str(jobs_dir / "sink-triage.log"),
        "SINK_TRIAGE_KPI": str(jobs_dir / "state" / "sink-triage-kpi.json"),
        "SINK_TRIAGE_CMD": cmd,
    }
    if ref is not None:
        env["SINK_TRIAGE_REF"] = ref
    if dry_run is not None:
        env["SINK_TRIAGE_DRY_RUN"] = dry_run
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SINK_TRIAGE)], capture_output=True, text=True, env=env, check=False
    )


def _kpi(jobs_dir: Path) -> dict | None:
    path = jobs_dir / "state" / "sink-triage-kpi.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.requirement("OPS-38")
def test_stale_worktree_is_repinned_to_origin_main_and_logged(tmp_path: Path) -> None:
    """[if] the worktree is stale [then] it is repinned to origin/main and logged, [else stop].

    [if] worktree stale at c1 and origin/main is at c2 [then] repinned to c2, logged."""
    origin, sha1, sha2 = _init_origin(tmp_path)
    wt = _stale_worktree(tmp_path, origin, sha1)
    jobs_dir = tmp_path / "jobs"

    result = _run(repo_root=wt, jobs_dir=jobs_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    head = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert head == sha2
    log = (jobs_dir / "sink-triage.log").read_text(encoding="utf-8")
    assert f"pinned ref=origin/main sha={sha2}" in log


@pytest.mark.requirement("OPS-38")
def test_dirty_tracked_file_refuses_without_touching_worktree(tmp_path: Path) -> None:
    """[if] a tracked file is dirty [then] it refuses and leaves HEAD alone, [else stop].

    [if] worktree has a tracked-file edit [then] refuses, logs why, leaves HEAD alone."""
    origin, sha1, _sha2 = _init_origin(tmp_path)
    wt = _stale_worktree(tmp_path, origin, sha1)
    (wt / "f.txt").write_text("dirty\n", encoding="utf-8")
    jobs_dir = tmp_path / "jobs"

    result = _run(repo_root=wt, jobs_dir=jobs_dir)

    assert result.returncode != 0
    head = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert head == sha1
    log = (jobs_dir / "sink-triage.log").read_text(encoding="utf-8")
    assert "ERROR refusing" in log
    assert "tracked changes" in log
    assert _kpi(jobs_dir) is None


def test_untracked_stray_does_not_block_repin(tmp_path: Path) -> None:
    """[if] worktree carries an untracked stray file (.venv-like) [then] repin still runs."""
    origin, sha1, sha2 = _init_origin(tmp_path)
    wt = _stale_worktree(tmp_path, origin, sha1)
    (wt / "stray-untracked.txt").write_text("not tracked\n", encoding="utf-8")
    jobs_dir = tmp_path / "jobs"

    result = _run(repo_root=wt, jobs_dir=jobs_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    head = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert head == sha2


@pytest.mark.requirement("OPS-38")
def test_fetch_failure_refuses_and_leaves_worktree_alone(tmp_path: Path) -> None:
    """[if] origin is unreachable [then] it refuses and leaves HEAD alone, [else stop].

    [if] origin remote is unreachable [then] refuses, logs why, leaves HEAD alone."""
    origin, sha1, _sha2 = _init_origin(tmp_path)
    wt = _stale_worktree(tmp_path, origin, sha1)
    missing_origin = tmp_path / "no-such-origin.git"
    subprocess.run(
        ["git", "-C", str(wt), "remote", "set-url", "origin", str(missing_origin)], check=True
    )
    jobs_dir = tmp_path / "jobs"

    result = _run(repo_root=wt, jobs_dir=jobs_dir)

    assert result.returncode != 0
    head = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert head == sha1
    log = (jobs_dir / "sink-triage.log").read_text(encoding="utf-8")
    assert "ERROR" in log
    assert "fetch" in log


def test_pinned_sha_ref_skips_fetch(tmp_path: Path) -> None:
    """[if] REF names a sha already present locally [then] no fetch needed, pins to it."""
    origin, sha1, sha2 = _init_origin(tmp_path)
    wt = _stale_worktree(tmp_path, origin, sha2)
    missing_origin = tmp_path / "no-such-origin.git"
    subprocess.run(
        ["git", "-C", str(wt), "remote", "set-url", "origin", str(missing_origin)], check=True
    )
    jobs_dir = tmp_path / "jobs"

    result = _run(repo_root=wt, jobs_dir=jobs_dir, ref=sha1)

    assert result.returncode == 0, result.stdout + result.stderr
    head = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert head == sha1
    log = (jobs_dir / "sink-triage.log").read_text(encoding="utf-8")
    assert f"pinned ref={sha1} sha={sha1}" in log


@pytest.mark.requirement("OPS-38")
def test_kpi_records_sha_and_preserves_existing_fields(tmp_path: Path) -> None:
    """[if] triage writes the kpi file [then] sha lands and old fields survive, [else stop].

    [if] triage succeeds and writes a kpi file [then] sha/ref merge in, old fields survive."""
    origin, sha1, sha2 = _init_origin(tmp_path)
    wt = _stale_worktree(tmp_path, origin, sha1)
    jobs_dir = tmp_path / "jobs"
    kpi_path = jobs_dir / "state" / "sink-triage-kpi.json"
    kpi_path.parent.mkdir(parents=True)
    kpi_path.write_text(
        json.dumps({"last_run": "2026-09-16T14:00:00Z", "new_issues": 5, "fingerprints": 2}),
        encoding="utf-8",
    )
    stub = tmp_path / "stub-cmd.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "cat > /dev/null\n"
        f"python3 - {shlex.quote(str(kpi_path))} <<'PY'\n"
        "import json, sys, pathlib\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "d = json.loads(p.read_text())\n"
        "d['new_issues'] = 7\n"
        "p.write_text(json.dumps(d))\n"
        "PY\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    result = _run(repo_root=wt, jobs_dir=jobs_dir, cmd=str(stub))

    assert result.returncode == 0, result.stdout + result.stderr
    kpi = _kpi(jobs_dir)
    assert kpi is not None
    assert kpi["sha"] == sha2
    assert kpi["ref"] == "origin/main"
    assert kpi["new_issues"] == 7
    assert kpi["fingerprints"] == 2


def test_failed_triage_run_does_not_stamp_kpi(tmp_path: Path) -> None:
    """[if] scripts.sink_triage exits nonzero [then] kpi file is not stamped with this run's sha."""
    origin, sha1, _sha2 = _init_origin(tmp_path)
    wt = _stale_worktree(tmp_path, origin, sha1)
    jobs_dir = tmp_path / "jobs"
    kpi_path = jobs_dir / "state" / "sink-triage-kpi.json"
    kpi_path.parent.mkdir(parents=True)
    kpi_path.write_text(
        json.dumps({"last_run": "2026-09-16T14:00:00Z", "sha": sha1}), encoding="utf-8"
    )

    result = _run(repo_root=wt, jobs_dir=jobs_dir, cmd=STUB_CMD_FAIL)

    assert result.returncode != 0
    kpi = _kpi(jobs_dir)
    assert kpi is not None
    assert kpi["sha"] == sha1


def test_dry_run_does_not_touch_kpi(tmp_path: Path) -> None:
    """[if] SINK_TRIAGE_DRY_RUN is set [then] kpi file is never created or stamped."""
    origin, sha1, _sha2 = _init_origin(tmp_path)
    wt = _stale_worktree(tmp_path, origin, sha1)
    jobs_dir = tmp_path / "jobs"

    result = _run(repo_root=wt, jobs_dir=jobs_dir, dry_run="1", cmd=STUB_CMD_OK)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _kpi(jobs_dir) is None


@pytest.fixture(scope="module", autouse=True)
def _require_sink_triage_script() -> None:
    assert SINK_TRIAGE.is_file(), f"missing {SINK_TRIAGE}"
