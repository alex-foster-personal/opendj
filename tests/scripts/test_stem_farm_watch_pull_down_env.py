"""Guard: scripts/stem_farm_watch_pull_down.sh fails loudly without connection
settings, rather than falling back to a hardcoded host/key/project (OSSPUB-01;
PR #4861 removed the real stem-farm IP and GCE project id that used to live
here as silent defaults).

This script is bash-only by nature (an operator task, per AGENTS.md's shell
policy), so these tests skip, naming the reason, on a host with no `bash` on
PATH (e.g. a bare Windows runner) rather than failing with FileNotFoundError.

  - [if] MDT_STEM_HOST/MDT_STEM_SSH_KEY/MDT_VOCAL_GCE_PROJECT are all unset
    [then] the script exits non-zero with a message naming the missing var,
    before attempting any ssh/gcloud call, [else broken]
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "stem_farm_watch_pull_down.sh"
)


def _bash() -> str:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("UNAVAILABLE: bash is required to run stem_farm_watch_pull_down.sh")
        raise AssertionError("unreachable: pytest.skip raises")  # narrows bash for the isolated mypy gate
    return bash


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_bash(), str(_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_missing_host_fails_fast_with_a_named_error() -> None:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("MDT_STEM_HOST", "MDT_STEM_SSH_KEY", "MDT_VOCAL_GCE_PROJECT")
    }
    result = _run(env)
    assert result.returncode != 0
    assert "MDT_STEM_HOST" in result.stderr


def test_missing_ssh_key_fails_fast_with_a_named_error() -> None:
    env = dict(os.environ)
    env.pop("MDT_STEM_SSH_KEY", None)
    env["MDT_STEM_HOST"] = "stem-farm.example.invalid"
    env["MDT_VOCAL_GCE_PROJECT"] = "example-project"
    result = _run(env)
    assert result.returncode != 0
    assert "MDT_STEM_SSH_KEY" in result.stderr


def test_missing_gce_project_fails_fast_with_a_named_error() -> None:
    env = dict(os.environ)
    env.pop("MDT_VOCAL_GCE_PROJECT", None)
    env["MDT_STEM_HOST"] = "stem-farm.example.invalid"
    env["MDT_STEM_SSH_KEY"] = "/dev/null"
    result = _run(env)
    assert result.returncode != 0
    assert "MDT_VOCAL_GCE_PROJECT" in result.stderr


_TOOLS = ("ssh", "scp", "uv", "gcloud")
# What the script itself needs from PATH besides the four programs it resolves.
_COREUTILS = ("dirname", "mkdir", "rm", "tail", "head", "tr", "date")


def _sandbox(tmp_path: Path, present: tuple[str, ...]) -> tuple[Path, dict[str, str], Path]:
    """A copy of the script under ``tmp_path`` and a PATH holding only coreutils
    and stand-ins for ``present``, each of which logs that it ran."""
    scripts = tmp_path / "repo" / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / _SCRIPT.name
    shutil.copy(_SCRIPT, script)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in _COREUTILS:
        real = shutil.which(tool)
        if real is None:
            pytest.skip(f"UNAVAILABLE: {tool} is required to run stem_farm_watch_pull_down.sh")
        else:
            (bin_dir / tool).symlink_to(real)
    log = tmp_path / "calls.log"
    for tool in present:
        # ssh answers as an idle farm, so the script goes straight to the pull.
        answer = 'printf "farm.log\\n---\\n0\\n0\\n"' if tool == "ssh" else ":"
        stand_in = bin_dir / tool
        stand_in.write_text(f'#!{_bash()}\necho "$0" >> "{log}"\n{answer}\n')
        stand_in.chmod(0o755)
    env = {
        "PATH": str(bin_dir),
        "MDT_STEM_HOST": "stem-farm.example.invalid",
        "MDT_STEM_SSH_KEY": "/dev/null",
        "MDT_VOCAL_GCE_PROJECT": "example-project",
    }
    return script, env, log


@pytest.mark.parametrize("missing", _TOOLS)
def test_a_program_missing_from_path_stops_the_script_before_anything_is_pulled(tmp_path: Path, missing: str) -> None:
    """[if] ssh, scp, uv or gcloud is not on PATH [then] the script exits non-zero naming it before running any of them, [else broken].

    A bare ``uv`` that a reduced PATH (cron, launchd) lacked stopped the script
    after the pull, with the VM still running (Codex on #6820). Control: with
    all four present the script runs each of them, in order, and exits 0.

    MUTATION TARGET: drop one ``resolve`` line (that program then fails only
    after the earlier ones ran).
    """
    if os.name == "nt":
        pytest.skip("UNAVAILABLE: the stand-ins are POSIX shell scripts and symlinks")
    script, env, log = _sandbox(tmp_path, tuple(tool for tool in _TOOLS if tool != missing))
    result = subprocess.run([_bash(), str(script)], env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode != 0
    assert f"{missing} is not on PATH" in result.stderr, result.stderr
    assert not log.exists(), "nothing runs before every program is resolved"


def test_control_with_every_program_present_each_runs_in_order(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("UNAVAILABLE: the stand-ins are POSIX shell scripts and symlinks")
    script, env, log = _sandbox(tmp_path, _TOOLS)
    result = subprocess.run([_bash(), str(script)], env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    bin_dir = tmp_path / "bin"
    # uv first: the importer's preflight runs before anything is pulled (Codex on #6821).
    assert log.read_text().split() == [str(bin_dir / tool) for tool in ("uv", "ssh", "scp", "uv", "gcloud")]
