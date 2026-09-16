"""SMARTEST-CI round 7: the miss audit's pure half, pinned.

Single-line intents:
  - if a broken test's file is outside a SCOPED plan's test paths yet counts as selected then broken
  - if the plan is SKIP_PYTEST and the audit counts any broken test selected then broken
  - if a test the ledger recorded over the ceiling reads as fast tier then broken
  - if a test main also broke in the window is counted against the plan then broken
  - if zero runs measured yields a recall number instead of UNKNOWN then broken

[if] a PR broke a test [then] the audit says whether the plan and fast tier ran it, [else stop].
"""

from __future__ import annotations

import pytest

from scripts.ci_miss_audit import (
    RunFailure,
    Selection,
    Tier,
    audit,
    changes_from_compare,
    selection_of,
    tier_of,
)
from scripts.ci_plan import Change, Config, Plan, Scope, Verdict

pytestmark = pytest.mark.requirement("OPS-16")

CONFIG = Config(
    full_triggers=("pyproject.toml",),
    always=("tests/*.py",),
    scopes=(
        Scope("engine", ("apps/engine_core/",), ("tests/engine_core/",)),
        Scope("library", ("apps/library/",), ("tests/library/",)),
    ),
)
LEDGER = {
    "tests/engine_core/test_a.py::test_fast": 0.01,
    "tests/library/test_b.py::test_slow": 3.2,
}


TRUNK = frozenset({"tests/engine_core/test_a.py::test_trunk"})


def _scoped(*paths: str) -> Plan:
    return Plan(Verdict.SCOPED, ("engine",), paths, "1 scope(s) touched")


# ----- selection -----


def test_an_identity_under_a_scoped_test_path_is_selected_and_one_outside_is_missed() -> None:
    plan = _scoped("tests/engine_core/", "tests/*.py")
    assert selection_of("tests/engine_core/test_a.py::test_x[1 2]", plan) is Selection.SELECTED
    assert selection_of("tests/test_root.py::test_y", plan) is Selection.SELECTED
    assert selection_of("tests/library/test_b.py::test_z", plan) is Selection.MISSED


def test_full_selects_everything_and_skip_pytest_selects_nothing() -> None:
    full = Plan(Verdict.FULL, (), (), "full trigger: pyproject.toml")
    skip = Plan(Verdict.SKIP_PYTEST, (), (), "docs only")
    assert selection_of("tests/library/test_b.py::test_z", full) is Selection.SELECTED
    assert selection_of("tests/library/test_b.py::test_z", skip) is Selection.MISSED


# ----- tier -----


def test_tier_reads_the_ledger_and_treats_an_unseen_test_as_fast() -> None:
    assert tier_of("tests/engine_core/test_a.py::test_fast", LEDGER, 0.5) is Tier.FAST
    assert tier_of("tests/library/test_b.py::test_slow", LEDGER, 0.5) is Tier.SLOW
    assert tier_of("tests/new/test_c.py::test_unseen", LEDGER, 0.5) is Tier.FAST


def test_a_truncated_parametrized_identity_has_an_unknown_tier() -> None:
    """control: the log parser stops at whitespace, so `[a b]` arrives as `[a`; never guess."""
    assert tier_of("tests/library/test_b.py::test_slow[a", LEDGER, 0.5) is Tier.UNKNOWN


# ----- compare payload -----


def test_github_compare_statuses_become_diff_letters() -> None:
    files = [
        {"status": "added", "filename": "apps/library/new.py"},
        {"status": "modified", "filename": "apps/engine_core/app.py"},
        {"status": "removed", "filename": "apps/library/old.py"},
        {
            "status": "renamed",
            "filename": "apps/library/b.py",
            "previous_filename": "apps/library/a.py",
        },
    ]
    assert changes_from_compare(files) == (
        Change("A", "apps/library/new.py"),
        Change("M", "apps/engine_core/app.py"),
        Change("D", "apps/library/old.py"),
        Change("R", "apps/library/a.py"),
        Change("R", "apps/library/b.py"),
    )


def test_an_unknown_compare_status_is_refused_not_guessed() -> None:
    with pytest.raises(ValueError, match="compare status"):
        changes_from_compare([{"status": "teleported", "filename": "x.py"}])


# ----- the audit -----


def _run(run_id: int, identities: set[str], *paths: str) -> RunFailure:
    return RunFailure(
        run_id=run_id,
        pr=100 + run_id,
        head=f"{run_id:07x}",
        identities=frozenset(identities),
        changes=tuple(Change("M", p) for p in paths),
        unmeasured_jobs=0,
    )


def test_recall_counts_only_pull_request_caused_failures_and_names_the_misses() -> None:
    runs = [
        # engine change broke an engine test (selected, fast) and a library test (MISSED, slow)
        _run(
            1,
            {"tests/engine_core/test_a.py::test_fast", "tests/library/test_b.py::test_slow"},
            "apps/engine_core/app.py",
        ),
        # library change broke a library test: selected; plus a trunk-red id that must not count
        _run(
            2,
            {"tests/library/test_b.py::test_slow", "tests/engine_core/test_a.py::test_trunk"},
            "apps/library/x.py",
        ),
    ]
    result = audit(runs, trunk_red=TRUNK, config=CONFIG, ledger=LEDGER, ceiling=0.5)
    assert result.pr_caused == 3
    assert result.trunk_red == 1
    assert result.plan_missed == ["run 1 (PR #101): tests/library/test_b.py::test_slow"]
    assert result.plan_recall == pytest.approx(2 / 3)
    assert result.fast_missed == [
        "run 1 (PR #101): tests/library/test_b.py::test_slow",
        "run 2 (PR #102): tests/library/test_b.py::test_slow",
    ]
    assert result.fast_recall == pytest.approx(1 / 3)


def test_a_run_whose_every_identity_is_trunk_red_measures_nothing_for_recall() -> None:
    runs = [_run(1, {"tests/engine_core/test_a.py::test_trunk"}, "apps/engine_core/app.py")]
    result = audit(runs, trunk_red=TRUNK, config=CONFIG, ledger=LEDGER, ceiling=0.5)
    assert result.pr_caused == 0
    assert result.plan_recall is None, "no denominator, no number"


def test_a_docs_only_diff_over_selects_today_so_the_audit_reports_no_miss() -> None:
    """control: the planner on main answers FULL for an unclaimed path (SKIP_PYTEST is
    unreachable, round 5), so a docs-only diff can never be a plan miss until that changes"""
    runs = [_run(1, {"tests/engine_core/test_a.py::test_fast"}, "docs/readme.md")]
    result = audit(runs, trunk_red=frozenset(), config=CONFIG, ledger=LEDGER, ceiling=0.5)
    assert result.rows[0].verdict is Verdict.FULL
    assert result.plan_missed == []
    assert result.plan_recall == 1.0


def test_the_verdict_distribution_exposes_a_vacuous_plan_recall() -> None:
    """control: FULL selects by construction, so 1.0 over FULL-only runs must say so"""
    runs = [
        _run(1, {"tests/engine_core/test_a.py::test_fast"}, "pyproject.toml"),
        _run(2, {"tests/engine_core/test_a.py::test_fast"}, "apps/engine_core/app.py"),
    ]
    result = audit(runs, trunk_red=frozenset(), config=CONFIG, ledger=LEDGER, ceiling=0.5)
    assert result.verdicts == {"FULL": 1, "SCOPED": 1}
    assert result.plan_recall == 1.0
