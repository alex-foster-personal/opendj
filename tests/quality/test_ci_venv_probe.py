"""tests/conftest.py's CI_VENV_PROBE must fail fast on a wrong interpreter,
scoped narrowly enough that it never fires outside that class (issue #3486).

The probe (conftest.py:_log_ci_venv_probe) used to be a bare ``print()``: it
named the wrong interpreter and the resulting missing module on every CI run
that hit it, and nothing ever read the line or failed the job. Three PRs sat
blocked behind a job whose real failure was buried under a diagnosis nobody
consumed.

Each case below drives a REAL pytest subprocess (not a unit test of the
helper) so the assertions are about the behavior CI actually depends on:

- ``test_fails_on_mismatched_interpreter_under_ci``: proves the PRESENCE of
  the new failure -- a CI session whose interpreter is not this repo's own
  venv now fails, naming both interpreters.
- ``test_passes_on_matching_interpreter_under_ci``: the negative control --
  a CI session using the real venv interpreter still passes, and the probe
  line is still printed (this did not become a silent gate).
- ``test_passes_when_deliberately_isolated_ci_session_opts_out``: the
  opposite-mutation guard -- the one CI step that runs pytest in a
  deliberately isolated, dependency-free uv environment
  (.github/workflows/ci.yml "Frontend typing-gate unit tests") sets
  CI_VENV_PROBE_ALLOW_MISMATCH=1 and must keep passing despite the mismatch.
- ``test_passes_on_mismatched_interpreter_outside_ci``: the other
  opposite-mutation guard -- a local developer run (no CI env var) must
  never start failing because of this, whatever its interpreter is.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_TARGET = "tests/quality/test_conftest_optional_starlette.py"

# Overrides sys.executable to a path that is provably not this repo's own
# venv, before pytest ever loads tests/conftest.py, then collects one real
# test module through the ordinary pytest hooks (pytest_collection_finish).
_COLLECT_WITH_FAKE_EXECUTABLE = """
import sys
sys.executable = "/nonexistent/decoy/bin/python"
import pytest
sys.exit(pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", sys.argv[1]]))
"""

_COLLECT_WITH_REAL_EXECUTABLE = """
import sys
import pytest
sys.exit(pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", sys.argv[1]]))
"""


def _run(script: str, env_overrides: dict[str, str | None]) -> subprocess.CompletedProcess[str]:
    import os

    env = dict(os.environ)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        [sys.executable, "-c", script, _TARGET],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_fails_on_mismatched_interpreter_under_ci() -> None:
    proc = _run(
        _COLLECT_WITH_FAKE_EXECUTABLE,
        {"CI": "true", "CI_VENV_PROBE_ALLOW_MISMATCH": None},
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "if a CI session under a wrong interpreter still exits 0, then "
        f"CI_VENV_PROBE's fail-fast guard did not fire:\n{output}"
    )
    assert "CI_VENV_PROBE" in output and "wrong interpreter" in output, (
        f"failure did not name the CI_VENV_PROBE mismatch:\n{output}"
    )
    assert "/nonexistent/decoy/bin/python" in output, (
        f"failure did not name the actual (wrong) interpreter:\n{output}"
    )
    assert str(REPO_ROOT / ".venv") in output, (
        f"failure did not name the expected repo venv:\n{output}"
    )


def test_passes_on_matching_interpreter_under_ci() -> None:
    proc = _run(
        _COLLECT_WITH_REAL_EXECUTABLE,
        {"CI": "true", "CI_VENV_PROBE_ALLOW_MISMATCH": None},
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "if a CI session on the real venv interpreter fails, then the guard "
        f"is too broad (rc={proc.returncode}):\n{output}"
    )
    assert "CI_VENV_PROBE" in output, (
        f"the probe line must still print on a passing session:\n{output}"
    )


def test_passes_when_deliberately_isolated_ci_session_opts_out() -> None:
    proc = _run(
        _COLLECT_WITH_FAKE_EXECUTABLE,
        {"CI": "true", "CI_VENV_PROBE_ALLOW_MISMATCH": "1"},
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "if the opt-out env var does not suppress the guard, then the one "
        "deliberately isolated CI step (frontend typing-gate) breaks "
        f"(rc={proc.returncode}):\n{output}"
    )


def test_passes_on_mismatched_interpreter_outside_ci() -> None:
    proc = _run(
        _COLLECT_WITH_FAKE_EXECUTABLE,
        {"CI": None, "CI_VENV_PROBE_ALLOW_MISMATCH": None},
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "if a non-CI (local developer) session fails because of this guard, "
        f"then it over-corrected past its intended scope (rc={proc.returncode}):\n{output}"
    )
