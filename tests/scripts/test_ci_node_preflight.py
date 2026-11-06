"""Every self-hosted node job runs the node preflight before `pnpm install`.

Regression lines:
  - if a job installs with pnpm without the preflight then a missing tool or
    a full disk fails mid-install with a cryptic pnpm error
  - if the preflight runs after the install then it guards nothing
  - if the script stops failing on a full disk then the tripwire is gone
  - if a poisoned frontend node_modules is left in place then the typing
    gate fails 84 times and `pnpm build` dies `vite: not found`
  - if repair keeps .pnpm-workspace-state-v1.json then pnpm short-circuits
    and the tree stays poisoned
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import stat
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCRIPT = REPO_ROOT / "scripts" / "ci_node_preflight.sh"
PREFLIGHT = "scripts/ci_node_preflight.sh apps/webui/frontend"
PREFLIGHT_PY = REPO_ROOT / "scripts" / "ci_node_preflight.py"

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


STATE = ".pnpm-workspace-state-v1.json"
TS_STORE = Path(".pnpm") / "typescript@1.0.0" / "node_modules" / "typescript"
KIT_STORE = Path(".pnpm") / "@sveltejs+kit@1.0.0" / "node_modules" / "@sveltejs" / "kit"
KIT_LINK_TARGET = "../.pnpm/@sveltejs+kit@1.0.0/node_modules/@sveltejs/kit"


def _write_pnpm_stub(path: Path, log: Path, *, mode: str) -> None:
    """`mode` is repair (relink a healthy tree), fail (exit 1), or leave-broken."""
    log_q = shlex.quote(str(log))
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"printf '%s\\n' \"$*\" >> {log_q}",
        f"if [ -e node_modules/{STATE} ]; then printf 'STATE_PRESENT\\n' >> {log_q}; fi",
    ]
    if mode == "repair":
        lines += [
            "mkdir -p node_modules/.pnpm/typescript@1.0.0/node_modules/typescript",
            "ln -sfn .pnpm/typescript@1.0.0/node_modules/typescript node_modules/typescript",
            f'mkdir -p "node_modules/{KIT_STORE.as_posix()}"',
            "mkdir -p node_modules/@sveltejs",
            f'ln -sfn "{KIT_LINK_TARGET}" node_modules/@sveltejs/kit',
            "mkdir -p node_modules/.bin",
            "printf '#!/usr/bin/env bash\\nexit 0\\n' > node_modules/.bin/vite",
            "chmod +x node_modules/.bin/vite",
            "exit 0",
        ]
    elif mode == "fail":
        lines.append("exit 1")
    else:
        lines.append("exit 0")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def _fake_env(tmp_path: Path, *, pnpm_mode: str = "repair") -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("corepack", "node"):
        (fake_bin / name).write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (fake_bin / name).chmod(0o755)
    pnpm_log = tmp_path / "pnpm-calls.log"
    _write_pnpm_stub(fake_bin / "pnpm", pnpm_log, mode=pnpm_mode)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_NAME": "probe-runner",
        "MDT_CI_MIN_FREE_GB": "0",
    }
    return env, pnpm_log


def _run_preflight(frontend: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), str(frontend)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _plant_healthy_tree(frontend: Path) -> Path:
    nm = frontend / "node_modules"
    store = nm / TS_STORE
    store.mkdir(parents=True)
    (nm / "typescript").symlink_to(TS_STORE)
    kit_store = nm / KIT_STORE
    kit_store.mkdir(parents=True)
    svelte_scope = nm / "@sveltejs"
    svelte_scope.mkdir(exist_ok=True)
    (svelte_scope / "kit").symlink_to(Path(KIT_LINK_TARGET))
    bin_dir = nm / ".bin"
    bin_dir.mkdir()
    vite = bin_dir / "vite"
    vite.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    vite.chmod(0o755)
    (nm / STATE).write_text("{}\n", encoding="utf-8")
    return nm


def test_absent_node_modules_skips_the_probe(tmp_path: Path) -> None:
    """if a fresh checkout with no tree yet is treated as poisoned then preflight
    demands pnpm before the workflow install that creates the tree"""
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    env, log = _fake_env(tmp_path)
    result = _run_preflight(frontend, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "probe-runner: corepack and node present" in result.stdout
    assert not log.exists()


def test_healthy_tree_does_not_call_pnpm_or_drop_workspace_state(tmp_path: Path) -> None:
    """if a good public layer still runs install then every job pays a relink
    and a green tree can lose its workspace-state file"""
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    nm = _plant_healthy_tree(frontend)
    env, log = _fake_env(tmp_path)
    result = _run_preflight(frontend, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "probe-runner: corepack and node present" in result.stdout
    assert not log.exists()
    assert (nm / STATE).exists()


def test_dangling_public_link_triggers_repair(tmp_path: Path) -> None:
    """if a dangling typescript link is left then the typing gate dies
    ERR_MODULE_NOT_FOUND before it reads a file"""
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    nm = _plant_healthy_tree(frontend)
    marker = nm / ".pnpm" / "keep-me"
    marker.write_text("stay\n", encoding="utf-8")
    (nm / "typescript").unlink()
    (nm / "typescript").symlink_to(".pnpm/missing/node_modules/typescript")
    env, log = _fake_env(tmp_path, pnpm_mode="repair")
    result = _run_preflight(frontend, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "repaired frontend node_modules" in result.stdout
    assert log.exists()
    recorded = log.read_text(encoding="utf-8")
    assert "install --frozen-lockfile" in recorded
    assert "STATE_PRESENT" not in recorded
    assert not (nm / STATE).exists()
    assert marker.exists()
    assert (nm / "typescript").exists()


def test_preflight_shell_invokes_python_module() -> None:
    """P1 ratchet: probe/repair lives in ci_node_preflight.py, not inline bash."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "ci_node_preflight.py" in text
    assert "_probe_frontend_node_modules" not in text
    assert "_list_symlinks" not in text


def _load_ci_node_preflight():
    spec = importlib.util.spec_from_file_location("ci_node_preflight", PREFLIGHT_PY)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_scoped_public_target_shape_helpers() -> None:
    """Scoped links must allow ../.pnpm/... only; unscoped .pnpm/... is poison under @scope."""
    m = _load_ci_node_preflight()
    assert m.is_scoped_public_target("../.pnpm/@sveltejs+kit@1.0.0/node_modules/@sveltejs/kit")
    assert not m.is_scoped_public_target(".pnpm/foo/node_modules/bar")
    assert not m.is_scoped_public_target("../../quality-gate-base-fake/x")


def test_dangling_scoped_public_link_triggers_repair(tmp_path: Path) -> None:
    """A bad @scope/pkg link alone must trip the probe (typescript and vite can look fine)."""
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    nm = _plant_healthy_tree(frontend)
    marker = nm / ".pnpm" / "keep-me"
    marker.write_text("stay\n", encoding="utf-8")
    kit = nm / "@sveltejs" / "kit"
    kit.unlink()
    kit.symlink_to("../.pnpm/missing/node_modules/@sveltejs/kit")
    env, log = _fake_env(tmp_path, pnpm_mode="repair")
    result = _run_preflight(frontend, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "poisoned link: @sveltejs/kit" in result.stderr
    assert "repaired frontend node_modules" in result.stdout
    assert log.exists()
    recorded = log.read_text(encoding="utf-8")
    assert "install --frozen-lockfile" in recorded
    assert "STATE_PRESENT" not in recorded
    assert not (nm / STATE).exists()
    assert marker.exists()
    assert kit.exists()


def test_escaped_scoped_public_link_triggers_repair(tmp_path: Path) -> None:
    """Live target outside ../.pnpm/... is still poison for scoped public links."""
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    nm = _plant_healthy_tree(frontend)
    outside = tmp_path / "quality-gate-base-fake" / "apps" / "webui" / "frontend" / "node_modules" / KIT_STORE
    outside.mkdir(parents=True)
    kit = nm / "@sveltejs" / "kit"
    kit.unlink()
    kit.symlink_to(os.path.relpath(outside, kit.parent))
    env, log = _fake_env(tmp_path, pnpm_mode="repair")
    result = _run_preflight(frontend, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "repaired frontend node_modules" in result.stdout
    recorded = log.read_text(encoding="utf-8")
    assert "install --frozen-lockfile" in recorded
    assert "STATE_PRESENT" not in recorded
    assert (nm / "@sveltejs" / "kit").exists()


def test_escaped_public_link_triggers_repair(tmp_path: Path) -> None:
    """if only dangling links are rejected then a live quality-gate-base-*
    target still rewrites the public layer and the next job inherits it"""
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    nm = _plant_healthy_tree(frontend)
    outside = tmp_path / "quality-gate-base-fake" / "apps" / "webui" / "frontend" / "node_modules" / TS_STORE
    outside.mkdir(parents=True)
    (nm / "typescript").unlink()
    (nm / "typescript").symlink_to(os.path.relpath(outside, nm))
    env, log = _fake_env(tmp_path, pnpm_mode="repair")
    result = _run_preflight(frontend, env)
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "install --frozen-lockfile" in recorded
    assert "STATE_PRESENT" not in recorded
    assert (nm / "typescript").exists()


def test_empty_bin_triggers_repair(tmp_path: Path) -> None:
    """if .bin exists with no vite then `pnpm build` dies `vite: not found`"""
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    nm = _plant_healthy_tree(frontend)
    vite = nm / ".bin" / "vite"
    vite.unlink()
    env, log = _fake_env(tmp_path, pnpm_mode="repair")
    result = _run_preflight(frontend, env)
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "install --frozen-lockfile" in recorded
    assert "STATE_PRESENT" not in recorded
    assert (nm / ".bin" / "vite").exists()


def test_repair_failure_when_pnpm_exits_1_names_the_runner(tmp_path: Path) -> None:
    """if the reinstall fails closed without ::error:: then the job still
    looks like 84 scanner RuntimeErrors"""
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    nm = _plant_healthy_tree(frontend)
    (nm / "typescript").unlink()
    (nm / "typescript").symlink_to(".pnpm/missing/node_modules/typescript")
    env, log = _fake_env(tmp_path, pnpm_mode="fail")
    result = _run_preflight(frontend, env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "::error::" in result.stderr
    assert "probe-runner" in result.stderr
    assert "install --frozen-lockfile" in log.read_text(encoding="utf-8")


def test_repair_failure_when_pnpm_leaves_the_tree_broken_names_the_runner(
    tmp_path: Path,
) -> None:
    """if a no-op install is treated as success then a poisoned runner stays
    poisoned for every later job"""
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    nm = _plant_healthy_tree(frontend)
    (nm / "typescript").unlink()
    (nm / "typescript").symlink_to(".pnpm/missing/node_modules/typescript")
    env, log = _fake_env(tmp_path, pnpm_mode="leave-broken")
    result = _run_preflight(frontend, env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "::error::" in result.stderr
    assert "probe-runner" in result.stderr
    assert "still poisoned after reinstall" in result.stderr
    assert "install --frozen-lockfile" in log.read_text(encoding="utf-8")
