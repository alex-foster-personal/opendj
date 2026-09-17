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
- ``test_soundfile_absent_in_isolated_ci_session``: expected optional-dep
  absence prints ``soundfile=absent``, not ``soundfile=ERROR``.
- ``test_soundfile_present_when_analysis_extra_installed``: a real importable
  ``soundfile`` prints ``soundfile=present``, distinguishing the two states.
- ``test_soundfile_error_on_broken_install``: a real ``soundfile`` wheel
  installed with ``uv pip --no-deps`` (missing transitive ``numpy``) still
  prints ``soundfile=ERROR``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

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


def _run(
    script: str,
    env_overrides: dict[str, str | None],
    *,
    python: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        [python or sys.executable, "-c", script, _TARGET],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )


def _probe_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("CI_VENV_PROBE")]


def _run_uv_pytest_collect(
    *,
    env_overrides: dict[str, str | None],
    extra_uv_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    cmd = [
        "uv",
        "run",
        "--no-project",
        "--isolated",
        "--python",
        "3.11",
        "--with",
        "pytest",
        *(extra_uv_args or []),
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        _TARGET,
    ]
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=180,
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


def test_soundfile_absent_in_isolated_ci_session() -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv is required to reproduce CI's isolated pytest environment")

    proc = _run_uv_pytest_collect(
        env_overrides={
            "CI": "true",
            "CI_VENV_PROBE_ALLOW_MISMATCH": "1",
        },
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "if the isolated CI typing-gate environment cannot collect, then the "
        f"absent-status probe cannot be measured (rc={proc.returncode}):\n{output}"
    )
    probe_lines = _probe_lines(output)
    assert probe_lines, f"CI_VENV_PROBE line missing from isolated session:\n{output}"
    assert any("soundfile=absent" in line for line in probe_lines), (
        f"expected soundfile=absent in probe output:\n{output}"
    )
    assert not any("soundfile=ERROR" in line for line in probe_lines), (
        f"expected absence must not read as ERROR:\n{output}"
    )


def test_soundfile_present_when_analysis_extra_installed() -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv is required to install the analysis extra for this probe")

    proc = _run_uv_pytest_collect(
        env_overrides={
            "CI": "true",
            "CI_VENV_PROBE_ALLOW_MISMATCH": "1",
        },
        extra_uv_args=["--with", "soundfile>=0.12"],
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "if soundfile cannot be installed for the present probe, then the "
        f"negative control cannot be measured (rc={proc.returncode}):\n{output}"
    )
    probe_lines = _probe_lines(output)
    assert probe_lines, f"CI_VENV_PROBE line missing from present session:\n{output}"
    assert any("soundfile=present" in line for line in probe_lines), (
        f"expected soundfile=present in probe output:\n{output}"
    )
    assert not any("soundfile=absent" in line for line in probe_lines), (
        f"present install must not read as absent:\n{output}"
    )


def _broken_soundfile_venv_python(tmp_root: Path) -> Path:
    """Return a venv python where soundfile is installed but not importable."""
    if shutil.which("uv") is None:
        pytest.skip("uv is required to reproduce a broken soundfile install")

    venv = tmp_root / "venv"
    subprocess.run(
        ["uv", "venv", str(venv), "--python", "3.11"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    python = venv / "bin" / "python"
    if not python.is_file():
        python = venv / "Scripts" / "python.exe"
    assert python.is_file(), f"expected venv python under {venv}"

    for packages in (["pytest"], ["soundfile>=0.12", "--no-deps"]):
        completed = subprocess.run(
            ["uv", "pip", "install", "--python", str(python), *packages],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, (
            "if the broken soundfile fixture cannot be provisioned, then the "
            f"fault probe cannot be measured:\n{completed.stdout}{completed.stderr}"
        )
    return python


def test_soundfile_error_on_broken_install() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        python = _broken_soundfile_venv_python(Path(tmp))
        proc = _run(
            _COLLECT_WITH_REAL_PREFIX,
            {
                "CI": None,
                "CI_VENV_PROBE_ALLOW_MISMATCH": None,
            },
            python=str(python),
        )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "if a broken soundfile install aborts collection, then the fault probe "
        f"cannot be measured (rc={proc.returncode}):\n{output}"
    )
    probe_lines = _probe_lines(output)
    assert probe_lines, f"CI_VENV_PROBE line missing from fault session:\n{output}"
    assert any(
        "soundfile=ERROR" in line and "ModuleNotFoundError" in line for line in probe_lines
    ), (
        f"broken install must still read as ERROR with exception detail:\n{output}"
    )
    assert not any("soundfile=absent" in line for line in probe_lines), (
        f"broken install must not be softened to absent:\n{output}"
    )
