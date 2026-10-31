"""Round 24: the recall probe scores what tests EXECUTED against what the planner selects.

[if] a module whose tests ran a changed file in the run phase is left out of the plan and the probe calls it selected [then] fail here, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import coverage
import pytest

from scripts.ci_plan import Config, Scope
from scripts.ci_plan_narrow import Reach
from scripts.ci_recall_probe import Miss, executed_pairs, sample_modules, score

pytestmark = pytest.mark.requirement("OPS-16")

# web's tests import core, so a change in core plans SCOPED with web narrowed by reach.
CONFIG = Config(
    (),
    ("tests/*.py",),
    (
        Scope("core", ("apps/core/",), ("tests/core/",), ("web",)),
        Scope("web", ("apps/web/",), ("tests/web/",)),
        Scope("x", ("apps/x/",), ("tests/x/",)),  # two idle scopes keep two under the cap
        Scope("y", ("apps/y/",), ("tests/y/",)),
    ),
)
_EMPTY = Reach(frozenset(), frozenset(), frozenset())


def test_each_test_context_becomes_a_module_file_pair_with_its_phases(tmp_path: Path) -> None:
    source = tmp_path / "apps" / "core" / "x.py"
    source.parent.mkdir(parents=True)
    source.write_text("a = 1\nb = 2\n")
    written = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
    for context, line in [
        ("tests/web/test_a.py::test_one|setup", 1),
        ("tests/web/test_a.py::test_two|run", 2),
        ("tests/core/test_b.py::TestC::test_d|teardown", 1),
        ("", 2),  # collection-time import: no test ran it
    ]:
        written.set_context(context)
        written.add_lines({str(source): [line]})
    written.write()
    read = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
    read.read()
    assert executed_pairs(read, tmp_path) == {
        ("tests/web/test_a.py", "apps/core/x.py"): frozenset({"setup", "run"}),
        ("tests/core/test_b.py", "apps/core/x.py"): frozenset({"teardown"}),
    }


def test_misses_are_unselected_scoped_pairs_split_by_phase() -> None:
    changed = "apps/core/x.py"
    pairs = {
        ("tests/core/test_core.py", changed): frozenset({"run"}),  # the changed scope's own: kept whole
        ("tests/web/test_reaches.py", changed): frozenset({"run"}),
        ("tests/web/test_blind.py", changed): frozenset({"setup", "run"}),
        ("tests/web/test_spill.py", changed): frozenset({"teardown"}),
        ("tests/web/test_reaches.py", "apps/unclaimed.py"): frozenset({"run"}),  # FULL: no scope claims it
    }
    index = {
        "tests/core/test_core.py": _EMPTY,
        "tests/web/test_reaches.py": Reach(frozenset({changed}), frozenset(), frozenset()),
        "tests/web/test_blind.py": _EMPTY,
        "tests/web/test_spill.py": _EMPTY,
    }
    got = score(pairs, CONFIG, index, frozenset())
    assert (got.pairs, got.scoped) == (5, 4)
    assert got.misses == (
        Miss("tests/web/test_blind.py", changed, frozenset({"setup", "run"})),
        Miss("tests/web/test_spill.py", changed, frozenset({"teardown"})),
    )
    assert [m.module for m in got.run_phase_misses] == ["tests/web/test_blind.py"]
    assert got.recall(len(got.run_phase_misses)) == 0.75


def test_a_guard_module_is_selected_whatever_its_reach() -> None:
    pair = {("tests/web/test_guard.py", "apps/core/x.py"): frozenset({"run"})}
    index = {"tests/web/test_guard.py": _EMPTY}
    assert score(pair, CONFIG, index, frozenset()).misses
    assert not score(pair, CONFIG, index, frozenset({"tests/web/test_guard.py"})).misses


def test_the_sample_is_seeded_and_keeps_only_quick_existing_test_modules(tmp_path: Path) -> None:
    quick = [f"tests/web/test_{i}.py" for i in range(6)]
    for module in [*quick, "tests/web/test_slow.py", "tests/scripts/test_planner.py"]:
        (tmp_path / module).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / module).write_text("")
    durations = {f"{m}::test": 0.5 for m in quick}
    durations |= {
        "tests/web/test_slow.py::test_a": 2.0,
        "tests/web/test_slow.py::test_b": 2.0,  # 4 s summed: over the limit
        "tests/scripts/test_planner.py::test": 0.1,  # the planner's own tests
        "tests/web/test_gone.py::test": 0.1,  # no such file
    }
    first = sample_modules(durations, 4, seed=24, root=tmp_path)
    assert first == sample_modules(durations, 4, seed=24, root=tmp_path)
    assert len(first) == 4 and set(first) <= set(quick)
    assert sample_modules(durations, 6, seed=24, root=tmp_path) == tuple(quick)
    with pytest.raises(ValueError, match="larger than population"):
        sample_modules(durations, 7, seed=24, root=tmp_path)  # the pool is the six quick modules only
