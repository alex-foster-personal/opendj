"""``scripts.pytest_tier_floor`` fails a tier run that measured nothing.

Each test runs a REAL pytest in a throwaway directory with the plugin loaded
the way ``just cloudsync-fast`` / ``just cloudsync-slow`` load it, and reads
the real exit code and output.

Single-line intent:
  - if an all-skipped run exits 0 or omits "executed 0" then broken
  - if a required tier that executed nothing passes because another tier ran then broken
  - if a run below the collect floor exits 0 then broken
  - if a run exactly at the collect floor fails then broken
  - if a misspelled required marker is accepted then broken
  - if the plugin changes the exit code when no option is given then broken
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("INFRA-03")

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

PYTEST_INI: str = """\
[pytest]
markers =
    tier_a: runs
    tier_b: skips
"""

SAMPLE_TESTS: str = """\
import pytest


@pytest.fixture
def absent_library():
    pytest.skip("no library on this host")


@pytest.mark.tier_a
def test_a_runs():
    assert True


@pytest.mark.tier_b
def test_b_skips_in_the_body():
    pytest.skip("no creds on this host")


@pytest.mark.tier_b
def test_b_skips_in_a_fixture(absent_library):
    assert True
"""


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "pytest.ini").write_text(PYTEST_INI, encoding="utf-8")
    (tmp_path / "test_sample.py").write_text(SAMPLE_TESTS, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "scripts.pytest_tier_floor",
            "--rootdir",
            str(tmp_path),
            "-c",
            str(tmp_path / "pytest.ini"),
            str(tmp_path),
            *args,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_an_all_skipped_run_fails_and_says_executed_0(tmp_path: Path) -> None:
    """if an all-skipped run exits 0 or omits "executed 0" then broken"""
    result = _run(tmp_path, "-m", "tier_b", "--tier-require-executed=tier_b")

    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout
    assert "FAIL: executed 0 of 2 selected" in result.stdout


def test_a_required_tier_that_executed_nothing_fails_even_when_another_ran(
    tmp_path: Path,
) -> None:
    """if a required tier that executed nothing passes because another tier ran then broken"""
    result = _run(tmp_path, "--tier-require-executed=tier_a", "--tier-require-executed=tier_b")

    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout
    assert "tier_a: executed 1 of 1 selected" in result.stdout
    assert "FAIL: tier 'tier_b' executed 0 of 2 selected" in result.stdout


def test_a_run_below_the_collect_floor_fails(tmp_path: Path) -> None:
    """if a run below the collect floor exits 0 then broken"""
    result = _run(tmp_path, "--tier-min-selected=4")

    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout
    assert "selected 3, below the floor of 4" in result.stdout


def test_a_run_at_the_collect_floor_that_executed_passes(tmp_path: Path) -> None:
    """if a run exactly at the collect floor fails then broken"""
    result = _run(tmp_path, "--tier-min-selected=3", "--tier-require-executed=tier_a")

    assert result.returncode == pytest.ExitCode.OK, result.stdout
    assert "all: executed 1 of 3 selected" in result.stdout
    assert "FAIL" not in result.stdout


def test_a_misspelled_required_marker_is_a_usage_error(tmp_path: Path) -> None:
    """if a misspelled required marker is accepted then broken"""
    result = _run(tmp_path, "--tier-require-executed=tier_c")

    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout
    assert "unregistered marker(s) ['tier_c']" in result.stderr


def test_without_options_the_plugin_leaves_the_exit_code_alone(tmp_path: Path) -> None:
    """if the plugin changes the exit code when no option is given then broken"""
    result = _run(tmp_path, "-m", "tier_b")

    assert result.returncode == pytest.ExitCode.OK, result.stdout
    assert "[tier-floor]" not in result.stdout
