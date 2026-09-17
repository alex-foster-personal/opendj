"""tests/conftest.py's CI_VENV_PROBE must fail fast on a wrong interpreter,
scoped narrowly enough that it never fires outside that class (issue #3486).

The probe (conftest.py:_log_ci_venv_probe) used to be a bare ``print()``: it
named the wrong interpreter and the resulting missing module on every CI run
that hit it, and nothing ever read the line or failed the job. Three PRs sat
blocked behind a job whose real failure was buried under a diagnosis nobody
consumed.

Each case below drives a REAL pytest subprocess (not a unit test of the
helper) so the assertions are about the behavior CI actually depends on:

- ``test_fails_on_mismatched_venv_under_ci``: proves the PRESENCE of the new
  failure -- a CI session whose venv root is not this repo's own now fails,
  naming both.
- ``test_fails_on_shared_interpreter_binary_with_a_different_venv_root``:
  Codex P1 on PR #3487 -- a POSIX ``uv``-managed venv's ``bin/python`` is a
  symlink into an interpreter binary shared across every venv on the
  machine, so comparing resolved *executable* paths lets an unrelated venv
  on the identical Python build alias as a match. This fakes only
  ``sys.prefix`` (the venv identity) while the real, shared
  ``sys.executable`` is untouched, and must still fail.
- ``test_passes_on_matching_venv_under_ci``: the negative control -- a CI
  session using the real venv still passes, and the probe line is still
  printed (this did not become a silent gate).
- ``test_passes_when_deliberately_isolated_ci_session_opts_out``: the
  opposite-mutation guard -- the one CI step that runs pytest in a
  deliberately isolated, dependency-free uv environment
  (.github/workflows/ci.yml "Frontend typing-gate unit tests") sets
  CI_VENV_PROBE_ALLOW_MISMATCH=1 and must keep passing despite the mismatch.
- ``test_passes_on_mismatched_venv_outside_ci``: the other
  opposite-mutation guard -- a local developer run (no CI env var) must
  never start failing because of this, whatever its venv is.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_TARGET = "tests/quality/test_conftest_optional_starlette.py"
_DECOY_VENV = "/nonexistent/decoy/venv"

# Overrides sys.prefix to a path that is provably not this repo's own venv,
# before pytest ever loads tests/conftest.py, then collects one real test
# module through the ordinary pytest hooks (pytest_collection_finish).
# sys.executable is left untouched on purpose: it stays the REAL, shared uv
# interpreter binary, which is exactly the aliasing case that defeated a
# resolved-executable comparison (Codex P1, PR #3487).
_COLLECT_WITH_FAKE_PREFIX = f"""
import sys
sys.prefix = {_DECOY_VENV!r}
import pytest
sys.exit(pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", sys.argv[1]]))
"""

_COLLECT_WITH_REAL_PREFIX = """
import sys
import pytest
sys.exit(pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", sys.argv[1]]))
"""


def _run(script: str, env_overrides: dict[str, str | None]) -> subprocess.CompletedProcess[str]:
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
        check=False,
        text=True,
        timeout=120,
    )


def test_fails_on_mismatched_venv_under_ci() -> None:
    proc = _run(
        _COLLECT_WITH_FAKE_PREFIX,
        {"CI": "true", "CI_VENV_PROBE_ALLOW_MISMATCH": None},
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "if a CI session under a wrong venv still exits 0, then "
        f"CI_VENV_PROBE's fail-fast guard did not fire:\n{output}"
    )
    assert "CI_VENV_PROBE" in output and "wrong interpreter" in output, (
        f"failure did not name the CI_VENV_PROBE mismatch:\n{output}"
    )
    assert _DECOY_VENV in output, (
        f"failure did not name the actual (wrong) venv:\n{output}"
    )
    assert str(REPO_ROOT / ".venv") in output, (
        f"failure did not name the expected repo venv:\n{output}"
    )


def test_fails_on_shared_interpreter_binary_with_a_different_venv_root() -> None:
    """Regression, PR #3487 Codex P1: a shared symlinked interpreter must not alias as a match."""
    real_python = REPO_ROOT / ".venv" / "bin" / "python"
    assert real_python.is_symlink(), (
        "this test's premise is a uv-managed venv whose bin/python is a "
        "symlink into a shared base interpreter -- re-check the layout if "
        "this no longer holds"
    )

    proc = _run(
        _COLLECT_WITH_FAKE_PREFIX,
        {"CI": "true", "CI_VENV_PROBE_ALLOW_MISMATCH": None},
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "if the guard passes merely because sys.executable resolves to the "
        "same shared uv interpreter binary as the real venv -- even though "
        "sys.prefix (the actual venv identity) does not match -- then it is "
        f"defeated by uv's interpreter sharing across venvs:\n{output}"
    )
    assert f"executable={str(real_python)!r}" in output, (
        "the failure message should still show the real (shared) executable "
        f"alongside the mismatched venv identity:\n{output}"
    )


def test_passes_on_matching_venv_under_ci() -> None:
    proc = _run(
        _COLLECT_WITH_REAL_PREFIX,
        {"CI": "true", "CI_VENV_PROBE_ALLOW_MISMATCH": None},
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "if a CI session on the real venv fails, then the guard is too broad "
        f"(rc={proc.returncode}):\n{output}"
    )
    assert "CI_VENV_PROBE" in output, (
        f"the probe line must still print on a passing session:\n{output}"
    )


def test_passes_when_deliberately_isolated_ci_session_opts_out() -> None:
    proc = _run(
        _COLLECT_WITH_FAKE_PREFIX,
        {"CI": "true", "CI_VENV_PROBE_ALLOW_MISMATCH": "1"},
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "if the opt-out env var does not suppress the guard, then the one "
        "deliberately isolated CI step (frontend typing-gate) breaks "
        f"(rc={proc.returncode}):\n{output}"
    )


def test_passes_on_mismatched_venv_outside_ci() -> None:
    proc = _run(
        _COLLECT_WITH_FAKE_PREFIX,
        {"CI": None, "CI_VENV_PROBE_ALLOW_MISMATCH": None},
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "if a non-CI (local developer) session fails because of this guard, "
        f"then it over-corrected past its intended scope (rc={proc.returncode}):\n{output}"
    )
