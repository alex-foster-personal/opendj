"""requires-python compared by BOUNDING RANGE, the way `uv lock --check` accepts an
existing lock (Codex P2 on #3763, round 54). Every row is a measurement on uv 0.8.17
against the six pair: the lock's `requires-python` replaced by `recorded`, the
pyproject's by `spelled`, `uv lock --check --offline` run; 0 is "Resolved", 1 is "The
lockfile ... needs to be updated". Comparing each clause by meaning (round 12) had
called `>=3.11,!=3.12.*` stale against `>=3.11` while uv kept the lock."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lock_metadata_check import EXIT_OK, EXIT_STALE, EXIT_UNKNOWN
from tests.scripts.test_lock_metadata_check import LOCK, PYPROJECT, _run


def _pair(tmp_path: Path, spelled: str, recorded: str) -> tuple[int, str]:
    pyproject = PYPROJECT.replace('requires-python = ">=3.11"', f'requires-python = "{spelled}"')
    lock = LOCK.replace('requires-python = ">=3.11"', f'requires-python = "{recorded}"')
    assert pyproject != PYPROJECT or lock != LOCK
    return _run(tmp_path, pyproject, lock)


@pytest.mark.parametrize(
    ("spelled", "recorded"),
    [
        (">=3.11,!=3.12.*", ">=3.11"),
        ("!=3.12.*,>=3.11", ">=3.11"),
        (">=3.11,!=3.11.2", ">=3.11"),
        (">=3.11,!=3.11.1,!=3.11.2", ">=3.11"),
        (">=3.11,!=3.12.*,!=3.13.*", ">=3.11"),
        (">=3.11,!=3.10.*", ">=3.11"),
        (">=3.11,!=4.*", ">=3.11"),
        (">=3.11,!=3.11.0.1", ">=3.11"),
        (">=3.11rc1", ">=3.11"),
        (">=3.11.0", ">=3.11"),
        (">= 3.11", ">=3.11"),
        (">=3.11,!=3.12.*,<4", ">=3.11, <4"),
        (">=3.11,!=3.12.*,!=3.13.*,<4", ">=3.11, <4"),
        (">=3.11,<4,!=3.12.0rc1", ">=3.11, <4"),
        (">=3.11,<4", ">=3.11, !=3.12.*, <4"),
        (">=3.11,!=3.13.*,<4", ">=3.11, !=3.12.*, <4"),
        (">=3.11,!=3.11.0,!=3.11.1", ">3.11.0"),
        (">=3.11,!=3.11.0", ">3.11.0, !=3.11.1"),
        (">=3.11,<3.12", "==3.11.*"),
        (">=3.11,!=3.12.*,<3.13", "==3.11.*"),
        (">=3.12,<3.13", "==3.12.*"),
        ("==3.11.*,!=3.11.5", "==3.11.*"),
        (">=3.11,!=3.11.*,<3.13", ">=3.12, <3.13"),
        ("~=3.11", ">=3.11, <4"),
        ("~=3.11.2", ">=3.11.2, <3.12"),
        (">=3.12,!=3.12.0.*", ">=3.12.1"),
        (">=3.11.0.0", ">=3.11"),
        (">=3.11,!=3.11.0.0", ">3.11.0"),
    ],
    ids=[
        "star-hole-unbounded",
        "star-hole-first",
        "point-hole",
        "two-point-holes",
        "two-star-holes",
        "hole-below-lower",
        "hole-above-everything",
        "four-component-point-hole",
        "pre-release-on-lower",
        "trailing-zero",
        "space-after-operator",
        "star-hole-bounded",
        "adjacent-star-holes-bounded",
        "pre-release-on-hole",
        "hole-only-in-lock",
        "different-holes",
        "point-hole-beside-exclusive-lower",
        "lock-keeps-a-hole-pyproject-lacks",
        "minor-window-as-star",
        "hole-empties-the-tail",
        "minor-window-3-12",
        "point-hole-in-star-window",
        "star-hole-moves-lower-with-upper",
        "compatible-release",
        "compatible-release-patch",
        "three-wide-star-hole-at-lower",
        "two-trailing-zeros",
        "two-trailing-zeros-on-hole",
    ],
)
def test_the_same_bounding_range_keeps_the_lock(
    tmp_path: Path, spelled: str, recorded: str
) -> None:
    """if the spelled requires-python admits the same lowest and highest bound as the
    lock's then 0, whatever `!=` holes, pre-release tags or spellings differ (uv 0.8.17
    exit 0 on each row, measured round 54)"""
    code, message = _pair(tmp_path, spelled, recorded)
    assert code == EXIT_OK, (spelled, recorded, message)


@pytest.mark.parametrize(
    ("spelled", "recorded"),
    [
        (">=3.11,!=3.11.0", ">=3.11"),
        (">=3.11.1,!=3.11.1", ">=3.11"),
        (">=3.11,!=3.11.*", ">=3.11"),
        (">=3.11,<4", ">=3.11"),
        (">3.10", ">=3.11"),
        (">=3.12", ">=3.11"),
        ("==3.11.*", ">=3.11"),
        ("~=3.11", ">=3.11"),
        (">=3.11", ">3.11.0"),
        (">=3.11,<4,!=3.11.0", ">=3.11, <4"),
        (">=3.11,!=3.13.*,<3.13", ">=3.11, <4"),
        (">=3.11,!=3.11.*,<3.13", ">=3.11"),
        (">=3.11,<=3.13", ">=3.11, <3.13"),
        (">=3.12,!=3.12.0.*", ">=3.13"),
        (">=3.12,!=3.12.0.*", ">=3.12"),
    ],
    ids=[
        "point-hole-at-lower",
        "point-hole-at-lower-patch",
        "star-hole-at-lower",
        "upper-added",
        "lower-exclusive-below",
        "lower-raised",
        "window-vs-open",
        "compatible-vs-open",
        "lock-lower-exclusive",
        "point-hole-at-lower-bounded",
        "star-hole-at-upper-tightens",
        "star-hole-at-lower-and-upper",
        "upper-inclusive-vs-exclusive",
        "three-wide-star-hole-is-not-a-minor",
        "three-wide-star-hole-moves-lower",
    ],
)
def test_a_hole_or_bound_that_moves_the_range_is_stale(
    tmp_path: Path, spelled: str, recorded: str
) -> None:
    """CONTROLS: a `!=` at either edge moves a bound, and a moved bound is a change (uv
    0.8.17 exit 1 on each row, measured round 54)"""
    code, message = _pair(tmp_path, spelled, recorded)
    assert code == EXIT_STALE, (spelled, recorded, message)
    assert "requires-python" in message, message


@pytest.mark.parametrize(
    ("spelled", "named"),
    [
        (">=3.11,<3.11", "admits no version"),
        (">=3.11,<4,===3.12", "arbitrary equality is not compared"),
        (">=1!3.11", "an epoch is not compared"),
    ],
    ids=["empty-range", "arbitrary-equality", "epoch"],
)
def test_a_range_uv_cannot_lock_is_unknown(tmp_path: Path, spelled: str, named: str) -> None:
    """`>=3.11,<3.11` is "Found conflicting Python requirements", exit 2 (measured
    round 54); `===` compares a raw string uv's release-only range does not model."""
    code, message = _pair(tmp_path, spelled, ">=3.11")
    assert code == EXIT_UNKNOWN, message
    assert named in message, message
