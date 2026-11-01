"""Round 24: the recall probe scores what tests EXECUTED against what the planner selects.

[if] a module whose tests ran a changed file in the run phase is left out of the plan and the probe calls it selected [then] fail here, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import coverage
import pytest

from scripts.ci_plan import Config, Scope
from scripts.ci_plan_narrow import Reach
from scripts.ci_recall_probe import (
    COLLECT,
    Miss,
    Rerun,
    collection_pairs,
    executed_pairs,
    other_modules_that_ran,
    sample_modules,
    sampleable_modules,
    score,
    split_by_rerun_alone,
    unusable_reruns,
)

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
    assert got.run_scoped == 3  # test_spill ran the file in teardown only
    assert got.scoped_recall == 0.5
    assert got.run_phase_recall == pytest.approx(2 / 3)


def test_fixture_only_pairs_never_move_run_phase_recall() -> None:
    """Codex on #8155: run-phase recall kept fixture-only pairs in its denominator, so a sample with
    more teardown spillover reported a higher recall without any run-phase selection changing."""
    changed = "apps/core/x.py"
    reaches = Reach(frozenset({changed}), frozenset(), frozenset())
    run_pairs = {
        ("tests/web/test_reaches.py", changed): frozenset({"run"}),
        ("tests/web/test_blind.py", changed): frozenset({"run"}),
    }
    spill = {(f"tests/web/test_spill_{i}.py", changed): frozenset({"teardown"}) for i in range(8)}
    index = {"tests/web/test_blind.py": _EMPTY} | {
        module: reaches for module, _ in [*run_pairs, *spill] if module != "tests/web/test_blind.py"
    }
    alone = score(run_pairs, CONFIG, index, frozenset())
    spilled = score(run_pairs | spill, CONFIG, index, frozenset())
    assert (alone.run_scoped, spilled.run_scoped) == (2, 2)
    assert alone.run_phase_recall == spilled.run_phase_recall == 0.5


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


def test_a_band_draws_only_modules_whose_summed_seconds_fall_in_it(tmp_path: Path) -> None:
    """Round 25: the slow suites the quick default never draws."""
    for module in ["tests/web/test_quick.py", "tests/web/test_slow.py", "tests/web/test_slower.py"]:
        (tmp_path / module).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / module).write_text("")
    durations = {
        "tests/web/test_quick.py::test": 0.5,
        "tests/web/test_slow.py::test_a": 2.0,
        "tests/web/test_slow.py::test_b": 2.0,  # 4 s summed
        "tests/web/test_slower.py::test": 12.0,
    }
    assert sample_modules(durations, 1, seed=25, root=tmp_path, band=(3.0, 10.0)) == ("tests/web/test_slow.py",)
    assert sample_modules(durations, 1, seed=25, root=tmp_path, band=(10.0, 30.0)) == ("tests/web/test_slower.py",)


def test_untimed_draws_the_test_modules_the_ledger_has_no_entry_for(tmp_path: Path) -> None:
    """Round 25: 404 of 1519 test modules had no entry in the committed `.test_durations` seed, so no band could draw them."""
    for module in ["tests/web/test_timed.py", "tests/web/test_new.py", "tests/scripts/test_new_planner.py"]:
        (tmp_path / module).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / module).write_text("")
    durations = {"tests/web/test_timed.py::test": 0.5}
    assert sample_modules(durations, 1, seed=25, root=tmp_path, band=None) == ("tests/web/test_new.py",)
    with pytest.raises(ValueError, match="larger than population"):
        sample_modules(durations, 2, seed=25, root=tmp_path, band=None)  # the planner's own tests stay out


def test_a_run_phase_miss_counts_only_if_its_module_reruns_the_file_alone() -> None:
    """Round 26: `test_store_schema.py` ran `state_maintenance.py` in the run phase only beside the
    module whose lifespan started that thread; alone it ran none of it."""
    kept = Miss("tests/web/test_a.py", "apps/core/x.py", frozenset({"run"}))
    fixture_alone = Miss("tests/web/test_b.py", "apps/core/x.py", frozenset({"setup", "run"}))
    gone_alone = Miss("tests/web/test_c.py", "apps/core/y.py", frozenset({"run"}))
    alone = {
        ("tests/web/test_a.py", "apps/core/x.py"): frozenset({"run"}),
        ("tests/web/test_b.py", "apps/core/x.py"): frozenset({"setup"}),
        ("tests/web/test_c.py", "apps/core/z.py"): frozenset({"run"}),
    }
    assert split_by_rerun_alone((kept, fixture_alone, gone_alone), alone) == ((kept,), (fixture_alone, gone_alone))


def test_a_spilled_miss_names_every_other_module_that_ran_its_file() -> None:
    spilled = Miss("tests/engine_core/test_store.py", "apps/core/x.py", frozenset({"run"}))
    pairs = {
        ("tests/engine_core/test_store.py", "apps/core/x.py"): frozenset({"run"}),
        ("tests/cloudsync/test_gate.py", "apps/core/x.py"): frozenset({"setup", "run"}),
        ("tests/web/test_late.py", "apps/core/x.py"): frozenset({"teardown"}),
        ("tests/web/test_other.py", "apps/core/y.py"): frozenset({"run"}),
    }
    assert other_modules_that_ran(spilled, pairs) == ("tests/cloudsync/test_gate.py", "tests/web/test_late.py")


def test_a_rerun_that_never_reached_its_module_is_no_evidence_of_spill() -> None:
    """Codex on #8182: a rerun that stopped at collection, `(2, {})`, turned the module's misses into
    SPILL and let the probe report 1.0000 with exit 0."""
    module = "tests/web/test_a.py"
    ran_it = {(module, "apps/core/x.py"): frozenset({"setup", "run"})}
    reruns = {
        "tests/web/test_ok.py": Rerun(0, {("tests/web/test_ok.py", "apps/core/x.py"): frozenset({"run"})}),
        "tests/web/test_red.py": Rerun(1, {("tests/web/test_red.py", "apps/core/x.py"): frozenset({"run"})}),
        "tests/web/test_collection.py": Rerun(2, {}),
        "tests/web/test_nothing.py": Rerun(5, {}),
        "tests/web/test_failed_mid_run.py": Rerun(
            2, {("tests/web/test_failed_mid_run.py", "apps/core/x.py"): frozenset({"run"})}
        ),
        "tests/web/test_setup_errors.py": Rerun(
            1, {("tests/web/test_setup_errors.py", "apps/core/x.py"): frozenset({"setup"})}
        ),
        module: Rerun(0, {("tests/web/test_other.py", "apps/core/x.py"): frozenset({"run"})}),
    }
    assert unusable_reruns(reruns) == (
        "tests/web/test_a.py",
        "tests/web/test_collection.py",
        "tests/web/test_failed_mid_run.py",
        "tests/web/test_nothing.py",
        "tests/web/test_setup_errors.py",
    )
    assert unusable_reruns({module: Rerun(0, ran_it)}) == ()


def test_collection_pairs_are_the_lines_a_module_collects_beyond_the_root_conftest() -> None:
    """Round 27: what importing a test module runs. The root conftest loads for every module, so its
    lines are no module's own; a line beyond it, in a new file or one the conftest also loaded, is."""
    baseline = {"apps/core/x.py": frozenset({1, 2})}
    collected = {
        "tests/web/test_imports_y.py": {"apps/core/x.py": frozenset({1, 2}), "apps/core/y.py": frozenset({1})},
        "tests/web/test_calls_x.py": {"apps/core/x.py": frozenset({1, 2, 9})},
        "tests/web/test_conftest_only.py": {"apps/core/x.py": frozenset({2})},
    }
    pairs = collection_pairs(baseline, collected)
    assert pairs == {
        ("tests/web/test_imports_y.py", "apps/core/y.py"): COLLECT,
        ("tests/web/test_calls_x.py", "apps/core/x.py"): COLLECT,
    }
    index = {module: _EMPTY for module in collected}
    got = score(pairs, CONFIG, index, frozenset())
    assert (got.scoped, got.run_scoped, len(got.run_phase_misses)) == (2, 2, 2)  # a broken import errors the module


def test_sampleable_modules_leave_out_the_planner_tests_and_duration_padding(tmp_path: Path) -> None:
    for module in [
        "tests/web/test_a.py",
        "tests/scripts/test_planner.py",
        "tests/_ci_dur_pad/test_pad.py",
        "tests/web/helper.py",
    ]:
        (tmp_path / module).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / module).write_text("")
    assert sampleable_modules(tmp_path) == ("tests/web/test_a.py",)
