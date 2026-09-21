"""SMARTEST-CI round 7: the miss audit's pure half, pinned.

Single-line intents:
  - if a broken test's file is outside a SCOPED plan's test paths yet counts as selected then broken
  - if the plan is SKIP_PYTEST and the audit counts any broken test selected then broken
  - if a test the ledger recorded over the ceiling reads as fast tier then broken
  - if a test main also broke in the window is counted against the plan then broken
  - if zero runs measured yields a recall number instead of UNKNOWN then broken
  - if a run listing whose newest run is days old is audited as the current window then broken

[if] a PR broke a test [then] the audit says whether the plan and fast tier ran it, [else stop].
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.ci_miss_audit import (
    MAX_WINDOW_AGE,
    RunFailure,
    Selection,
    Tier,
    audit,
    changes_from_compare,
    node_id,
    selection_of,
    stale_window_reason,
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


# ----- the production identity format (Codex P1, Sol P1s) -----


def test_identities_keep_the_log_prefix_and_are_matched_by_node_id() -> None:
    """control: `scripts.ci_failure_ids` yields `FAILED tests/...`; the ledger and the plan
    start at `tests/`, and comparing unstripped read every failure as an unseen fast test"""
    assert (
        node_id("FAILED tests/library/test_b.py::test_slow") == "tests/library/test_b.py::test_slow"
    )
    assert node_id("ERROR tests/x.py::test_e") == "tests/x.py::test_e"
    assert tier_of("FAILED tests/library/test_b.py::test_slow", LEDGER, 0.5) is Tier.SLOW
    plan = _scoped("tests/engine_core/")
    assert selection_of("FAILED tests/engine_core/test_a.py::test_x", plan) is Selection.SELECTED
    assert selection_of("FAILED tests/library/test_b.py::test_z", plan) is Selection.MISSED


def test_an_unknown_plan_leaves_the_plan_denominator_but_not_the_fast_one() -> None:
    """control: identities a run could not plan are not hits; an unreadable diff is UNKNOWN"""
    unreadable_diff = RunFailure(
        1, 101, "0000001", frozenset({"FAILED tests/library/test_b.py::test_slow"}), None, 0
    )
    planned = _run(2, {"FAILED tests/engine_core/test_a.py::test_fast"}, "apps/engine_core/app.py")
    result = audit(
        [unreadable_diff, planned], trunk_red=frozenset(), config=CONFIG, ledger=LEDGER, ceiling=0.5
    )
    assert result.rows[0].verdict is None and "could not be read" in result.rows[0].reason
    assert result.pr_caused == 2
    assert result.plan_unmeasured == 1
    assert result.plan_denominator == 1 and result.plan_recall == 1.0
    assert result.fast_denominator == 2 and result.fast_recall == pytest.approx(0.5)


def test_a_run_with_an_unreadable_pytest_job_is_excluded_whole() -> None:
    """control: the unread job may hold exactly the slow or unselected failure being audited"""
    partial = RunFailure(
        1,
        101,
        "0000001",
        frozenset({"FAILED tests/engine_core/test_a.py::test_fast"}),
        (Change("M", "apps/engine_core/app.py"),),
        1,
    )
    result = audit([partial], trunk_red=frozenset(), config=CONFIG, ledger=LEDGER, ceiling=0.5)
    assert result.unreadable_runs == 1 and result.unreadable_identities == 1
    assert result.pr_caused == 0
    assert result.plan_recall is None and result.fast_recall is None


def test_an_unreadable_main_job_makes_both_recalls_unknown() -> None:
    """control: trunk red is incomplete, so a PR-caused count built on it is not a number"""
    runs = [_run(1, {"FAILED tests/engine_core/test_a.py::test_fast"}, "apps/engine_core/app.py")]
    result = audit(
        runs,
        trunk_red=frozenset(),
        config=CONFIG,
        ledger=LEDGER,
        ceiling=0.5,
        trunk_unreadable_jobs=1,
    )
    assert result.pr_caused == 1
    assert result.plan_recall is None and result.fast_recall is None


def test_an_always_fast_entry_makes_a_slow_recorded_test_fast_tier() -> None:
    """control: the audit reads the same list the tier plugin forces, by node id or by file"""
    slow = "FAILED tests/library/test_b.py::test_slow"
    assert tier_of(slow, LEDGER, 0.5) is Tier.SLOW
    assert (
        tier_of(slow, LEDGER, 0.5, frozenset({"tests/library/test_b.py::test_slow"})) is Tier.FAST
    )
    assert tier_of(slow, LEDGER, 0.5, frozenset({"tests/library/test_b.py"})) is Tier.FAST
    assert (
        tier_of(slow, LEDGER, 0.5, frozenset({"tests/library/test_b.py::test_other"})) is Tier.SLOW
    )


# ----- window recency -----
# Mon 21 Sep 2026: the runs listing answered with early-September runs for one call and
# current ones fifteen minutes later; the audit printed 48 / 48 over the stale window.

NOW = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)


def _runs(*ages: timedelta) -> list[dict]:
    return [{"created_at": (NOW - age).strftime("%Y-%m-%dT%H:%M:%SZ")} for age in ages]


def test_a_listing_whose_newest_run_is_older_than_the_limit_is_refused_by_name() -> None:
    reason = stale_window_reason(_runs(timedelta(days=12), timedelta(days=13)), now=NOW)
    assert reason is not None and "2026-09-09" in reason


def test_a_listing_with_a_recent_run_is_accepted() -> None:
    assert stale_window_reason(_runs(timedelta(hours=1), timedelta(days=12)), now=NOW) is None


def test_the_limit_is_exact_at_its_boundary() -> None:
    assert stale_window_reason(_runs(MAX_WINDOW_AGE), now=NOW) is None
    assert stale_window_reason(_runs(MAX_WINDOW_AGE + timedelta(seconds=1)), now=NOW)


def test_an_empty_listing_is_refused_not_read_as_a_quiet_week() -> None:
    assert stale_window_reason([], now=NOW) == "the listing returned no runs"
