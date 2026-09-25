"""Whitespace inside a specifier token is a parse error to uv, never a spelling.

`> =3.11` and `>=3 .11` are "Failed to parse" for requires-python and "must be pep508"
for a dependency, `uv lock --check` exit 2 (measured uv 0.8.17, Codex P2 on #3763,
round 49); deleting every space had collapsed them onto the valid spelling the lock was
made from. Whitespace AROUND an operator or operand is grammar (`>= 3.11`, ` >= 3.11 `,
`six >= 1` are read, exit 0), and stays a match."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lock_metadata_check import EXIT_OK, EXIT_UNKNOWN
from tests.scripts.test_lock_metadata_check import _run
from tests.scripts.test_lock_metadata_types import UNMARKED_LOCK, UNMARKED_PYPROJECT

REQUIRES = 'requires-dist = [{ name = "six" }]'
BOUND = 'requires-dist = [{ name = "six", specifier = ">=1" }]'


def _pair(python: str = ">=3.11", dependency: str = "six") -> tuple[str, str]:
    assert UNMARKED_PYPROJECT.count('requires-python = ">=3.11"') == 1
    assert UNMARKED_PYPROJECT.count('dependencies = ["six"]') == 1
    pyproject = UNMARKED_PYPROJECT.replace(
        'requires-python = ">=3.11"', f'requires-python = "{python}"'
    ).replace('dependencies = ["six"]', f'dependencies = ["{dependency}"]')
    assert UNMARKED_LOCK.count(REQUIRES) == 1
    lock = UNMARKED_LOCK if dependency == "six" else UNMARKED_LOCK.replace(REQUIRES, BOUND)
    return pyproject, lock


@pytest.mark.parametrize(
    ("python", "dependency", "named"),
    [
        ("> =3.11", "six", "'> =3.11'"),
        (">=3 .11", "six", "'>=3 .11'"),
        (">=3.11", "six> =1", "'> =1'"),
        (">=3.11", "six>=1 .0", "'>=1 .0'"),
        ("= 3.11", "six", "'= 3.11'"),
    ],
    ids=["python-op-split", "python-version-split", "dep-op-split", "dep-version-split", "no-op"],
)
def test_whitespace_inside_a_specifier_token_is_unknown(
    tmp_path: Path, python: str, dependency: str, named: str
) -> None:
    code, message = _run(tmp_path, *_pair(python, dependency))
    assert code == EXIT_UNKNOWN, message
    assert "whitespace inside a specifier token" in message, message
    assert named in message, message


@pytest.mark.parametrize(
    ("python", "dependency"),
    [(">= 3.11", "six"), (" >= 3.11 ", "six"), (">=3.11", "six >= 1"), (">=3.11", "six>=1")],
    ids=["python-space-between", "python-space-around", "dep-space-between", "dep-plain"],
)
def test_whitespace_around_a_specifier_token_still_matches(
    tmp_path: Path, python: str, dependency: str
) -> None:
    """CONTROLS: each pair is `uv lock --check` exit 0 (measured uv 0.8.17, round 49)."""
    code, message = _run(tmp_path, *_pair(python, dependency))
    assert code == EXIT_OK, message
