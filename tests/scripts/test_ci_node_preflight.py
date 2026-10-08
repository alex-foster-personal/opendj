"""Every self-hosted node job runs the node preflight before `pnpm install`.

Regression lines:
  - if a job installs with pnpm without the preflight then a missing tool or
    a full disk fails mid-install with a cryptic pnpm error
  - if the preflight runs after the install then it guards nothing
  - if the script stops failing on a full disk then the tripwire is gone
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCRIPT = REPO_ROOT / "scripts" / "ci_node_preflight.sh"
PREFLIGHT = "scripts/ci_node_preflight.sh apps/webui/frontend"

#: Linux jobs that run `pnpm install`; macOS packaging is hosted and has no
#: shared store, and GNU df is not there either.
NODE_JOBS = (
    ("ci.yml", "frontend-build"),
    ("ci.yml", "frontend"),
    ("ci.yml", "quality"),
    ("ci.yml", "contracts"),
    ("e2e.yml", None),
)


def _steps(workflow: str, job_id: str | None) -> list[tuple[str, list[dict]]]:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    jobs = doc["jobs"]
    if job_id is not None:
        return [(job_id, jobs[job_id]["steps"])]
    return [
        (jid, job["steps"])
        for jid, job in jobs.items()
        if any("pnpm install" in (s.get("run") or "") for s in job.get("steps") or [])
    ]


def test_every_pnpm_install_is_preceded_by_the_node_preflight() -> None:
    """if the preflight is missing or late then the store is unguarded"""
    for workflow, job_id in NODE_JOBS:
        for jid, steps in _steps(workflow, job_id):
            runs = [s.get("run") or "" for s in steps]
            installs = [i for i, r in enumerate(runs) if "pnpm install" in r]
            preflights = [i for i, r in enumerate(runs) if PREFLIGHT in r]
            assert installs, f"{workflow}:{jid} has no pnpm install; drop it from NODE_JOBS"
            assert preflights, f"{workflow}:{jid} installs without {PREFLIGHT}"
            assert min(preflights) < min(installs), f"{workflow}:{jid}: preflight after install"


def test_preflight_script_is_executable_and_fails_closed_on_a_full_disk(tmp_path: Path) -> None:
    """if the disk floor stops failing then a full runner reds mid-install again"""
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "scripts/ci_node_preflight.sh is not executable"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("corepack", "node"):
        (fake_bin / name).write_text("#!/usr/bin/env bash\nexit 0\n")
        (fake_bin / name).chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "RUNNER_NAME": "probe-runner"}

    # An impossible floor stands in for a full disk: the check has to fail.
    result = subprocess.run(
        [str(SCRIPT), str(tmp_path)],
        env={**env, "MDT_CI_MIN_FREE_GB": "999999"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "probe-runner has" in result.stderr and "below the 999999G floor" in result.stderr

    # CONTROL: a zero floor passes with the same harness and names the runner.
    result = subprocess.run(
        [str(SCRIPT), str(tmp_path)],
        env={**env, "MDT_CI_MIN_FREE_GB": "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "probe-runner: corepack and node present" in result.stdout


def test_the_live_engine_acceptance_runs_after_a_frontend_install() -> None:
    """if the job that arms AutoPlay through the frontend client installs no
    node_modules in apps/webui/frontend then the test passes or fails on what an
    earlier job left in that runner's workspace (nucbox-wsl-11, Thu 8 Oct 2026:
    esbuild not found)"""
    needle = "tests/health_lights/test_ahead_analysis_live_engine.py"
    frontend = "apps/webui/frontend"
    doc = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    users = []
    for jid, job in doc["jobs"].items():
        steps = job.get("steps") or []
        runs = [s.get("run") or "" for s in steps]
        # A shard that passes --ignore=<needle> does not run it.
        uses = [i for i, r in enumerate(runs) if needle in r.replace(f"--ignore={needle}", "")]
        if not uses:
            continue
        users.append(jid)
        job_dir = ((job.get("defaults") or {}).get("run") or {}).get("working-directory") or ""
        installs = [
            i for i, s in enumerate(steps) if _installs_into(runs[i], s.get("working-directory") or job_dir, frontend)
        ]
        assert installs, f"ci.yml:{jid} runs {needle} with no install into {frontend}"
        assert min(installs) < min(uses), f"ci.yml:{jid}: {frontend} install after {needle}"
    assert users, f"no ci.yml job runs {needle}; this guard is measuring nothing"


def _commands(run: str) -> list[str]:
    """A run block's command lines: blank lines and shell comments dropped."""
    lines = (line.strip() for line in run.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _installs_into(run: str, working_directory: str, target: str) -> bool:
    """True when a step's frozen-lockfile install lands in `target`: by its
    working directory, or by pnpm's own -C / --dir argument.

    A static tripwire, not a shell interpreter. It reads each line of the run
    block as one command and counts only a line that BEGINS with the install,
    so a commented-out or echoed install does not count. It does not evaluate
    a step's `if:` condition or shell control flow around the command."""
    in_target = working_directory.removeprefix("./").rstrip("/") == target
    for command in _commands(run):
        if command.startswith("pnpm install --frozen-lockfile") and in_target:
            return True
        if any(command.startswith(f"pnpm {flag} {target} install --frozen-lockfile") for flag in ("-C", "--dir")):
            return True
    return False


def test_an_install_outside_the_frontend_workspace_does_not_count() -> None:
    """if a root or other-workspace install satisfies the guard above then
    apps/webui/frontend/node_modules can be absent while the guard stays green"""
    frontend = "apps/webui/frontend"
    install = "pnpm install --frozen-lockfile"
    assert _installs_into(install, frontend, frontend)
    assert _installs_into(install, f"./{frontend}/", frontend)
    assert _installs_into(f"corepack enable\n  {install}\n", frontend, frontend)
    assert _installs_into(f"pnpm -C {frontend} install --frozen-lockfile", "", frontend)
    assert _installs_into(f"pnpm --dir {frontend} install --frozen-lockfile", "", frontend)
    assert not _installs_into(install, "", frontend)
    assert not _installs_into(install, "apps/desktop", frontend)
    assert not _installs_into("pnpm -C apps/desktop install --frozen-lockfile", "", frontend)
    assert not _installs_into("echo no install here", frontend, frontend)


def test_a_commented_or_echoed_install_does_not_count() -> None:
    """if the install is commented out or only printed then no node_modules is
    created, and a guard that matched the text anywhere would stay green"""
    frontend = "apps/webui/frontend"
    install = "pnpm install --frozen-lockfile"
    assert not _installs_into(f"# {install}", frontend, frontend)
    assert not _installs_into(f"  #{install}\necho done", frontend, frontend)
    assert not _installs_into(f'echo "{install}"', frontend, frontend)
    assert not _installs_into(f"# pnpm -C {frontend} install --frozen-lockfile", "", frontend)
    assert _installs_into(f"# deps for the live-engine client\n{install}", frontend, frontend)
