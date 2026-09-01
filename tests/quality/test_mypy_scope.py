"""The mypy ratchet is only honest if its SCOPE and its TOOL cannot drift.

`ops/quality/baseline.json` records a mypy error count. That number is a
comparison against a value someone else's machine wrote down, so two things
have to be nailed shut or the comparison is meaningless:

  1. THE SCOPE. Errors fall as the code improves, and they also fall when the
     gate stops looking at half the tree. `mypy.files_checked` is the ungated
     control that makes the second case visible, but a control is a detector,
     not a prevention. These tests are the prevention: the scanned set lives in
     ONE place (`[tool.mypy] files` in pyproject.toml), it must equal the set
     ruff lints, and `scripts/quality_gate.py` must not smuggle its own paths
     onto the command line where they could quietly disagree.

  2. THE TOOL. mypy adds and removes checks between versions exactly as ruff
     does, and this repo has already been bitten by that once with ruff
     (tests/quality/test_toolchain_pins.py: 0.7.4 and 0.16.3 were not stricter
     and looser versions of one question, they were different questions). The
     measurement env pins mypy in ops/quality/mypy-requirements.txt; the dev
     extra carries a copy so an editor or an ad-hoc run uses the same checker.
     Two pins means drift, so the drift is what gets tested.

The third guard is the excludes. `apps/desktop/src-tauri/payload/` is a
gitignored ~100MB staging tree holding a relocatable CPython and the whole
installed dependency closure, written by `just dmg`. It exists on a machine
that has built the dmg and nowhere else, so a gate that scanned it would score
the same commit differently on two hosts. radon hit this first and crashed on
numpy's .pxd files; mypy would merely lie.

Regression lines:
  - if [tool.mypy] files stops matching CFG.PY_LINT_PATHS then mypy and ruff
    are scoring different trees, so broken
  - if scripts/quality_gate.py passes paths to mypy then pyproject.toml is no
    longer the single source of truth for the scope, so broken
  - if the mypy pin in ops/quality/mypy-requirements.txt stops matching the one
    in pyproject's dev extra then a local run no longer predicts the gate,
    so broken
  - if a vendored or derived prefix is dropped from [tool.mypy] exclude then
    the count becomes host-dependent, so broken
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from scripts import quality_gate as qg

pytestmark = pytest.mark.requirement("TYPING-01")

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
MYPY_REQS = REPO_ROOT / "ops" / "quality" / "mypy-requirements.txt"

_MYPY_PIN = re.compile(r"^mypy==(?P<version>[0-9]+\.[0-9]+(?:\.[0-9]+)?)\s*$", re.MULTILINE)


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _mypy_config() -> dict:
    return _pyproject()["tool"]["mypy"]


def test_mypy_scans_exactly_what_ruff_lints() -> None:
    """One Python scope, named once, or the two gates measure different trees."""
    assert tuple(_mypy_config()["files"]) == qg.CFG.PY_LINT_PATHS, (
        "if [tool.mypy] files drifts from CFG.PY_LINT_PATHS then ruff and mypy "
        "score different subsets of the same repo, and 'the Python interior' "
        f"means two things: mypy has {_mypy_config()['files']}, ruff has "
        f"{list(qg.CFG.PY_LINT_PATHS)}"
    )


def test_the_gate_hands_mypy_no_paths_of_its_own() -> None:
    """The scope lives in pyproject.toml; the gate must not restate it."""
    smuggled = [p for p in qg.CFG.PY_LINT_PATHS if p in qg.CFG.MYPY_FLAGS]
    assert not smuggled, (
        f"if scripts/quality_gate.py passes {smuggled} to mypy then the command "
        "line silently overrides [tool.mypy] files, and narrowing the scope in "
        "one place stops showing up in the other"
    )


def test_the_measurement_pins_mypy_exactly() -> None:
    """A ratcheted count needs the same checker today and next month."""
    match = _MYPY_PIN.search(MYPY_REQS.read_text(encoding="utf-8"))
    assert match, (
        f"if {MYPY_REQS.name} has no exact `mypy==` pin then the gate measures "
        "with whatever mypy shipped that morning, and the baseline compares "
        "two different questions"
    )


def test_the_dev_extra_carries_the_same_mypy_as_the_gate() -> None:
    """A local `mypy` that is not the gate's mypy predicts nothing."""
    gate_pin = _MYPY_PIN.search(MYPY_REQS.read_text(encoding="utf-8"))
    assert gate_pin, "the gate pin must parse before it can be compared"
    dev = _pyproject()["project"]["optional-dependencies"]["dev"]
    dev_pins = [d for d in dev if d.replace(" ", "").startswith("mypy==")]
    assert len(dev_pins) == 1, (
        f"if the dev extra does not carry exactly one `mypy==` pin then a "
        f"contributor's mypy is not the gate's mypy; found {dev_pins}"
    )
    dev_version = dev_pins[0].replace(" ", "").split("==", 1)[1]
    assert dev_version == gate_pin.group("version"), (
        f"mypy pin drift: the dev extra pins {dev_version} but "
        f"{MYPY_REQS.name} pins {gate_pin.group('version')}. mypy adds and "
        "removes checks between versions, so these disagreeing means a clean "
        "local run no longer predicts the CI ratchet."
    )


@pytest.mark.parametrize("prefix", (*qg.CFG.VENDORED, *qg.CFG.DERIVED))
def test_every_unscored_prefix_is_excluded_from_mypy(prefix: str) -> None:
    """Vendored and derived trees must not reach mypy on any host."""
    excludes = _mypy_config()["exclude"]
    assert any(prefix in pattern for pattern in excludes), (
        f"if {prefix} is not in [tool.mypy] exclude then mypy scores it "
        f"wherever it happens to exist on disk; excludes are {excludes}"
    )
