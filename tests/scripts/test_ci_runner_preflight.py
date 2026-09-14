"""Issue #1041: a self-hosted CI job must fail fast, naming the runner and the
missing executable, before it does real work -- not die mid-job with a bare
`command not found`.

The bug this pins: CI Cost Guard called `python` with no
actions/setup-python step, no apt-install and no preflight. On a self-hosted
Ubuntu 24.04 runner missing python-is-python3, that died 33+ steps into the
job's own logic with exit 127 and no mention of the runner or which tool was
absent. scripts/ci_runner_preflight.sh is the fix's CI-side half: a tripwire
step that workflows relying on a bare interpreter call BEFORE their real
work, so a mis-provisioned runner fails on step one with a message naming
both what is missing and where to fix it.

REAL SCRIPT, REAL PATH MANIPULATION. Nothing here is mocked: the "missing
executable" case is produced by handing the real script a PATH that
genuinely does not contain the tool, the same shape as
tests/scripts/test_dmg_preflight.py uses for the same reason (AGENTS.md,
"No mocks and locked real fixtures"). The tool is hidden with
`path_hiding` (tests/scripts/_hermetic_path.py), not by dropping its whole
PATH directory: python often shares /usr/bin with bash, env and coreutils,
and dropping that entire directory to hide python would also hide whatever
else lives there, depending on the runner's own layout rather than on the
code under test (the same class PR #2624 fixed for doppler).

Regression lines:
  - if every named executable is present and the script still exits nonzero
    then broken
  - if an executable is absent and the script exits 0 then broken
  - if an executable is absent and the failure message does not name the
    runner then broken
  - if an executable is absent and the failure message does not name which
    executable is missing then broken
  - if called with no executables named and it exits 0 then broken
  - if hiding python also hides a tool that shares its directory (bash,
    env, coreutils) then the run fails for the wrong reason, not the one
    under test
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.scripts._hermetic_path import path_hiding

REPO = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO / "scripts" / "ci_runner_preflight.sh"


def _run(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(PREFLIGHT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_present_executable_passes() -> None:
    env = {**os.environ, "RUNNER_NAME": "test-runner"}
    result = _run(["sh"], env=env)
    assert result.returncode == 0, result.stderr
    assert "ok: runner 'test-runner'" in result.stdout


def test_missing_executable_fails_naming_runner_and_tool() -> None:
    tool = "an-executable-that-truly-does-not-exist"
    env = {**os.environ, "RUNNER_NAME": "agentbox-2", "PATH": os.environ.get("PATH", "")}
    result = _run([tool], env=env)
    assert result.returncode == 1
    assert "agentbox-2" in result.stderr
    assert tool in result.stderr


def test_missing_python_on_a_real_stripped_path_fails_for_the_right_reason(
    tmp_path: Path,
) -> None:
    """The exact shape of #1041: `python` absent from a real, unmodified PATH.

    Every other PATH entry (and every other executable in the directories
    that do provide python) is left intact by path_hiding, so this stays a
    real, close-to-unmodified PATH minus exactly python -- not a synthetic
    minimal one.
    """
    if (Path("/usr/bin") / "python").exists():
        pytest.skip("this host has /usr/bin/python; cannot exercise the missing case for real")
    env = {**os.environ, "RUNNER_NAME": "agentbox-1", "PATH": path_hiding(tmp_path, "python")}
    result = _run(["python"], env=env)
    assert result.returncode == 1
    assert "agentbox-1" in result.stderr
    assert "python" in result.stderr


def test_mixed_present_and_missing_reports_only_the_missing_one() -> None:
    tool = "an-executable-that-truly-does-not-exist"
    env = {**os.environ, "RUNNER_NAME": "test-runner"}
    result = _run(["sh", tool], env=env)
    assert result.returncode == 1
    reported = result.stderr.split("missing required executable(s):")[-1]
    assert tool in reported
    assert "sh" not in reported.split(tool)[0]


def test_no_executables_named_is_a_usage_error_not_a_silent_pass() -> None:
    result = _run([], env=dict(os.environ))
    assert result.returncode == 2
