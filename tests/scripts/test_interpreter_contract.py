"""Interpreter floor contract regression tests.

These drive the real module in a real subprocess against real pyproject
files, so they fail when the capability is removed rather than when a string
in the source drifts.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.interpreter_contract import (
    DEFAULT_PYPROJECT,
    InterpreterContractError,
    parse_floor,
    read_requires_python,
)

MODULE = Path(__file__).resolve().parents[2] / "scripts" / "interpreter_contract.py"
RUNNING = ".".join(str(part) for part in sys.version_info[:3])


def _write_pyproject(root: Path, specifier: str | None) -> Path:
    body = '[project]\nname = "probe"\nversion = "0.0.0"\n'
    if specifier is not None:
        body += f'requires-python = "{specifier}"\n'
    path = root / "pyproject.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _run(pyproject: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MODULE), "--pyproject", str(pyproject)],
        capture_output=True,
        text=True,
        check=False,  # a non-zero exit is the thing under test
    )


def test_interpreter_below_the_floor_fails_loudly(tmp_path: Path) -> None:
    """If the interpreter is below the declared floor then the check must fail."""
    result = _run(_write_pyproject(tmp_path, ">=99.0"))

    assert result.returncode == 2
    assert RUNNING in result.stderr
    assert ">=99.0" in result.stderr
    assert sys.executable in result.stderr


def test_interpreter_meeting_the_floor_passes(tmp_path: Path) -> None:
    """If the interpreter satisfies the floor then the check must stay quiet."""
    result = _run(_write_pyproject(tmp_path, ">=3.0"))

    assert result.returncode == 0
    assert "[OK]" in result.stdout


def test_unparseable_specifier_fails_rather_than_passing(tmp_path: Path) -> None:
    """If the floor cannot be parsed then it must fail, never pass unchecked."""
    result = _run(_write_pyproject(tmp_path, ">=3.11,<4"))

    assert result.returncode == 2
    assert "not a bare" in result.stderr


def test_missing_requires_python_fails_rather_than_passing(tmp_path: Path) -> None:
    """If no floor is declared then the check must say so, not pass silently."""
    result = _run(_write_pyproject(tmp_path, None))

    assert result.returncode == 2
    assert "no requires-python" in result.stderr


def test_this_suite_runs_on_an_interpreter_the_repo_allows() -> None:
    """If the suite itself runs below the repo floor then that must be red."""
    result = _run(DEFAULT_PYPROJECT)

    assert result.returncode == 0, result.stderr


def test_floor_parser_accepts_the_repo_specifier() -> None:
    """If the repo's own specifier stops parsing then the guard is inert."""
    assert parse_floor(read_requires_python(DEFAULT_PYPROJECT)) == (3, 11)


def test_floor_parser_refuses_a_range() -> None:
    """If a range slipped through then an unchecked interpreter would run."""
    with pytest.raises(InterpreterContractError):
        parse_floor(">=3.11,<4")
