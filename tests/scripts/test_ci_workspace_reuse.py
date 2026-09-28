"""Dependency trees survive between jobs on the persistent self-hosted runners.

Every self-hosted job used to check out with `clean: true`, which deleted
node_modules, .venv and every cargo target dir, and then rebuilt them. On a
shared host that churn was a main cause of disk I/O saturation (nucbox, Thu
10 Sep 2026: io PSI full avg60 23-43%). A job that installs nothing still
wiped the trees for the next job on that runner, so the rule has to cover
every self-hosted checkout, not only the ones that install.

Requirements:
- ✔︎ Every self-hosted checkout keeps dependency trees and removes all other
  untracked and ignored paths right after checking out.
- ✔︎ A reused .venv is recreated when its interpreter is not the one a fresh
  job would build, and kept otherwise.
- ✔︎ A reused .venv is installed exactly, so it holds what a fresh fill would.
- ✔︎ Every self-hosted checkout fetches full history (`fetch-depth: 0`), so no
  job leaves a shallow graft in a workspace the next job reuses.

Acceptance tests:
- [if] a self-hosted checkout goes back to `clean: true`, or loses the clean
  step right after it [then] this fails [⛔️ if one job can wipe the trees
  every other job on its runner reuses].
- [if] the clean step stops removing build debris (dist/, .svelte-kit/, .env,
  a nested venv) [then] this fails [⛔️ if one branch's outputs leak into the
  next branch's run].
- [if] the clean step starts removing node_modules, the root .venv or a cargo
  target dir [then] this fails [⛔️ if the churn silently comes back].
- [if] a self-hosted job runs bare `uv venv` [then] this fails [⛔️ uv refuses
  to create over an existing venv, so a reused workspace would go red].
- [if] a reused venv's install drops `--exact` or `--upgrade` [then] this fails
  [⛔️ if a reused venv tests another branch's packages or stale versions].
- [if] the venv's interpreter reports another version, or does not run
  [then] it is recreated from the requested interpreter.
- [if] the venv's interpreter matches [then] it is kept, contents intact.
- [if] a self-hosted checkout drops `fetch-depth: 0` (or sets any other depth)
  [then] this fails [⛔️ a depth-1 fetch writes .git/shallow, and the next
  full-history job on that runner pays an `--unshallow` re-download: measured
  6.1 GB of the 11.2 GB of pack writes on nucbox in 48 h, ADR on the branch
  af--ci-partial-clone].
- [if] the selector stops finding the history-dependent jobs (ci.yml test,
  fast, contracts, quality; adr-check) [then] this fails [⛔️ the depth rule
  would pass vacuously for the jobs whose results depend on history].
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
CLEAN_SCRIPT = REPO_ROOT / "scripts" / "ci_clean_untracked.sh"
VENV_SCRIPT = REPO_ROOT / "scripts" / "ci_venv.sh"
SELF_HOSTED_VAR_PREFIX = "vars.CI_RUNS_ON_"
CHECKOUT_PREFIX = "actions/checkout@"
CLEAN_STEP_RUN = "scripts/ci_clean_untracked.sh"
FULL_HISTORY = 0
# Jobs whose results read history: progress-tree provenance, the 90-day
# hotspot report, and the origin/main ancestry guard. A positive control for
# the depth rule's selector.
HISTORY_DEPENDENT = {
    ("ci.yml", "test"), ("ci.yml", "fast"), ("ci.yml", "contracts"),
    ("ci.yml", "quality"), ("adr-check.yml", "gate"),
}
EXACT_INSTALL = "uv pip install --exact --upgrade --python .venv/bin/python "
PYTHON = "3.11"


#-----------------------------------------------------------------------------
# helpers
#-----------------------------------------------------------------------------
def _self_hosted_jobs() -> list[tuple[str, str, list[dict]]]:
    """(workflow, job, steps) for every job that can land on a persistent workspace."""
    found = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (doc.get("jobs") or {}).items():
            if SELF_HOSTED_VAR_PREFIX in str(job.get("runs-on", "")):
                found.append((path.name, job_name, job.get("steps") or []))
    return found


def _checkout_jobs() -> list[tuple[str, str, list[dict]]]:
    return [
        (w, j, steps)
        for w, j, steps in _self_hosted_jobs()
        if any(str(s.get("uses", "")).startswith(CHECKOUT_PREFIX) for s in steps)
    ]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _touch(root: Path, *relpaths: str) -> None:
    for rel in relpaths:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")


def _run_venv_script(
    workdir: Path,
    *extra_args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    run_env = os.environ.copy()
    run_env.pop("VIRTUAL_ENV", None)
    if env is not None:
        run_env.update(env)
    return subprocess.run(
        ["bash", str(VENV_SCRIPT), PYTHON, *extra_args],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
        env=run_env,
    )


def _offline_uv_env(cache_dir: Path, home: Path) -> dict[str, str]:
    """Keep uv on the real interpreter and cache, with no network or downloads."""
    interpreter_dir = Path(sys.executable).resolve().parent
    uv_dir = Path(shutil.which("uv") or "").resolve().parent
    return {
        "PATH": f"/usr/bin:/bin:/usr/sbin:/sbin:{interpreter_dir}:{uv_dir}",
        "HOME": str(home),
        "UV_CACHE_DIR": str(cache_dir),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_OFFLINE": "1",
    }


def _installed_distribution_names(python: Path) -> set[str]:
    code = (
        "import importlib.metadata as m\n"
        "for dist in sorted(m.distributions(), key=lambda d: d.metadata['Name'].lower()):\n"
        "    print(dist.metadata['Name'].lower())\n"
    )
    result = subprocess.run(
        [str(python), "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in result.stdout.splitlines() if line}


def _version_of(python: Path) -> str:
    return subprocess.run(
        [str(python), "-c", "import platform; print(platform.python_version())"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _wanted_version() -> str:
    base = subprocess.run(
        ["uv", "python", "find", "--system", PYTHON], capture_output=True, text=True, check=True
    ).stdout.strip()
    return _version_of(Path(base))


def _fake_interpreter(workdir: Path, body: str) -> None:
    """A .venv whose python is a shell stub, with no pyvenv.cfg.

    Runs both recreate branches without a second real interpreter, and in the
    harshest shape: uv will not `--clear` a directory that is not a venv.
    """
    python = workdir / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    python.chmod(0o755)
    (workdir / ".venv" / "MARKER").write_text("stale", encoding="utf-8")


#-----------------------------------------------------------------------------
# structural policy
#-----------------------------------------------------------------------------
def test_selector_finds_the_self_hosted_checkouts() -> None:
    """Guards the rest: a selector that matches nothing proves nothing."""
    jobs = _checkout_jobs()
    assert len(jobs) >= 16, f"expected >=16 self-hosted checkout jobs, found {len(jobs)}"


@pytest.mark.parametrize("workflow,job", [(w, j) for w, j, _ in _checkout_jobs()])
def test_checkout_keeps_dependency_trees_and_cleans_right_after(workflow: str, job: str) -> None:
    steps = next(s for w, j, s in _checkout_jobs() if (w, j) == (workflow, job))
    idx = next(i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith(CHECKOUT_PREFIX))
    assert steps[idx].get("with", {}).get("clean") is False, (
        f"{workflow}:{job} checkout must set clean: false so node_modules/.venv/target survive"
    )
    after = steps[idx + 1] if idx + 1 < len(steps) else {}
    assert str(after.get("run", "")).strip() == CLEAN_STEP_RUN, (
        f"{workflow}:{job} step after checkout must run {CLEAN_STEP_RUN}, got {after!r}"
    )


def _checkout_with(steps: list[dict]) -> dict:
    step = next(s for s in steps if str(s.get("uses", "")).startswith(CHECKOUT_PREFIX))
    return step.get("with") or {}


def test_depth_selector_covers_the_history_dependent_jobs() -> None:
    """A depth rule whose selector misses the jobs that need history proves nothing."""
    selected = {(w, j) for w, j, _ in _checkout_jobs()}
    missing = HISTORY_DEPENDENT - selected
    assert not missing, f"history-dependent jobs fell out of the self-hosted selector: {missing}"


@pytest.mark.parametrize("workflow,job", [(w, j) for w, j, _ in _checkout_jobs()])
def test_checkout_fetches_full_history_so_no_shallow_graft_is_left(workflow: str, job: str) -> None:
    """One depth for every job that shares a persistent workspace.

    actions/checkout v4 runs `git fetch --depth=1` for the default depth, which
    writes .git/shallow into the reused repository. The next `fetch-depth: 0`
    job on that runner then fetches with `--unshallow`, which re-downloads
    history the runner already holds (up to ~150 MB per fetch on a runner
    without a blob filter). A uniform depth means the workspace is never
    shallow, and every fetch is an incremental negotiation against local refs.
    """
    steps = next(s for w, j, s in _checkout_jobs() if (w, j) == (workflow, job))
    depth = _checkout_with(steps).get("fetch-depth")
    assert depth == FULL_HISTORY, (
        f"{workflow}:{job} checkout has fetch-depth={depth!r}; every self-hosted checkout "
        f"must set fetch-depth: {FULL_HISTORY} so it never leaves a shallow workspace behind"
    )


def test_self_hosted_jobs_never_run_bare_uv_venv() -> None:
    """uv refuses to create over an existing venv, and the workspace now keeps one."""
    offenders = [
        f"{w}:{j}:{s.get('name')}"
        for w, j, steps in _self_hosted_jobs()
        for s in steps
        if re.search(r"^\s*uv venv\b", str(s.get("run", "")), re.M)
    ]
    assert not offenders, f"use scripts/ci_venv.sh instead of bare uv venv: {offenders}"


def test_every_reused_venv_is_installed_exactly() -> None:
    provisioning = [
        (w, j, str(s["run"]))
        for w, j, steps in _self_hosted_jobs()
        for s in steps
        if "scripts/ci_venv.sh" in str(s.get("run", ""))
    ]
    # CONTROL: ci.yml (3 jobs), full-ci, docs and release-check provision this way.
    assert len(provisioning) >= 6, provisioning
    for w, j, run in provisioning:
        assert f"scripts/ci_venv.sh {PYTHON}" in run, f"{w}:{j}"
        assert EXACT_INSTALL in run, f"{w}:{j} must install with --exact --upgrade: {run}"


#-----------------------------------------------------------------------------
# behavior: the clean script, run for real against a real git repo
#-----------------------------------------------------------------------------
KEPT = (
    "node_modules/pkg/index.js",
    "apps/webui/frontend/node_modules/.pnpm/lock.yaml",
    ".venv/bin/python",
    "apps/desktop/src-tauri/target/debug/app",
    "apps/webui/server/native/waveform/target/release/lib.so",
)
REMOVED = (
    "dist/music_dj_tools-0.0.0-cp311-abi3.whl",
    "dist/waveform-consumer/bin/python",
    "apps/webui/frontend/.svelte-kit/generated/root.js",
    "apps/webui/frontend/build/index.html",
    ".env",
    "tools/nested/.venv/bin/python",
    "stray-untracked.txt",
)


def test_clean_script_removes_debris_and_keeps_dependency_trees(tmp_path: Path) -> None:
    repo = tmp_path / "ws"
    repo.mkdir()
    _git(repo, "init", "-q")
    _touch(repo, "tracked.txt", ".gitignore")
    (repo / ".gitignore").write_text("dist/\n.env\nbuild/\n.svelte-kit/\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=ci@example.invalid", "-c", "user.name=ci", "commit", "-qm", "init")
    _touch(repo, *KEPT, *REMOVED)

    result = subprocess.run(
        ["bash", str(CLEAN_SCRIPT)], cwd=repo, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    kept_missing = [p for p in KEPT if not (repo / p).exists()]
    debris_left = [p for p in REMOVED if (repo / p).exists()]
    assert not kept_missing, f"clean removed dependency trees it must keep: {kept_missing}"
    assert not debris_left, f"clean left build debris behind: {debris_left}"
    assert (repo / "tracked.txt").exists(), "clean must never touch tracked files"


#-----------------------------------------------------------------------------
# behavior: the venv script, run for real with uv
#-----------------------------------------------------------------------------
needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")


@needs_uv
def test_venv_script_creates_a_missing_venv(tmp_path: Path) -> None:
    result = _run_venv_script(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "[venv] recreating .venv (missing)" in result.stdout
    assert _version_of(tmp_path / ".venv" / "bin" / "python") == _wanted_version()


@needs_uv
def test_venv_script_reuses_a_matching_venv_untouched(tmp_path: Path) -> None:
    assert _run_venv_script(tmp_path).returncode == 0
    marker = tmp_path / ".venv" / "MARKER"
    marker.write_text("kept", encoding="utf-8")

    result = _run_venv_script(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "[venv] reusing .venv" in result.stdout
    assert marker.read_text(encoding="utf-8") == "kept", "matching venv must be reused, not rebuilt"


@needs_uv
@pytest.mark.parametrize(
    "stub,reason",
    [("echo 3.10.0", "python 3.10.0, want"), ("exit 3", "interpreter does not run")],
)
def test_venv_script_recreates_a_wrong_or_broken_interpreter(
    tmp_path: Path, stub: str, reason: str
) -> None:
    _fake_interpreter(tmp_path, stub)

    result = _run_venv_script(tmp_path)

    assert result.returncode == 0, result.stderr
    assert reason in result.stdout, result.stdout
    assert not (tmp_path / ".venv" / "MARKER").exists(), "mismatched venv must be rebuilt"
    assert _version_of(tmp_path / ".venv" / "bin" / "python") == _wanted_version()


@needs_uv
def test_venv_script_installs_exactly_with_offline_venv(tmp_path: Path) -> None:
    """--sync must leave only lockfile packages, even when reusing a polluted venv."""
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "uv-cache"
    home = tmp_path / "home"
    home.mkdir()
    offline = _offline_uv_env(cache, home)

    pyproject = """\
[project]
name = "ci-venv-exactness"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["certifi"]
"""
    (project / "pyproject.toml").write_text(pyproject, encoding="utf-8")

    subprocess.run(["uv", "lock"], cwd=project, check=True, env=offline | {"UV_OFFLINE": "0"})
    subprocess.run(["uv", "sync"], cwd=project, check=True, env=offline | {"UV_OFFLINE": "0"})

    expected = _installed_distribution_names(project / ".venv" / "bin" / "python")
    assert "certifi" in expected, f"seed sync must install certifi, got {sorted(expected)}"

    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(project / ".venv" / "bin" / "python"),
            "idna",
        ],
        cwd=project,
        check=True,
        env=offline | {"UV_OFFLINE": "0"},
    )
    polluted = _installed_distribution_names(project / ".venv" / "bin" / "python")
    assert polluted != expected, "pollution step must change the venv before the exact sync"

    result = _run_venv_script(project, "--sync", env=offline)
    assert result.returncode == 0, result.stderr or result.stdout

    actual = _installed_distribution_names(project / ".venv" / "bin" / "python")
    assert actual == expected, (
        f"offline --sync must match lockfile exactly: "
        f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
    )


def test_scripts_are_executable() -> None:
    for script in (CLEAN_SCRIPT, VENV_SCRIPT):
        assert os.access(script, os.X_OK), f"{script} must be executable; workflows run it"
