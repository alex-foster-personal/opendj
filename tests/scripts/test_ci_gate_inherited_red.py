"""`ci gate` passes a pull request whose only failing tests are main's, under strict guards.

Regression lines:
  - if every failure is inherited and a repair is open but the gate stays red then broken
  - if one failure is not on main and the gate passes then broken
  - if no ci:trunk-repair pull request is open and the gate passes then broken
  - if the diff touches a failing test's file and the gate passes then broken
  - if main's red cannot be read and the gate passes then broken (must fail closed)
  - if a waived pass prints no ::warning:: naming the inherited ids then broken
  - if a failure in any job other than `test` is waived then broken
"""

from __future__ import annotations

from scripts.ci_gate_inherited_red import (
    Evidence,
    Unmeasured,
    Waiver,
    decide_waiver,
    failing_test_file,
    only_test_job_failed,
    try_waiver,
)
from scripts.ci_required_gate import Verdict, apply_inherited_red

A = "FAILED tests/audio/test_a.py::test_one"
B = "FAILED tests/ui/test_b.py::test_two[x - y]"
NEW = "FAILED tests/new/test_c.py::test_three"
RED_TEST = Verdict(False, ("scope: ...", "ok   frontend: success", "FAIL test: failure (in scope, must succeed)"))


def _evidence(**overrides: object) -> Evidence:
    base = {
        "pr_ids": frozenset({A, B}),
        "main_ids": frozenset({A, B, NEW.replace("test_three", "other")}),
        "repair_prs": (9001,),
        "changed_files": frozenset({"scripts/foo.py"}),
    }
    base.update(overrides)
    return Evidence(**base)  # type: ignore[arg-type]


def test_all_inherited_with_a_repair_open_passes_loudly() -> None:
    waiver = decide_waiver(_evidence())
    assert waiver.granted
    assert waiver.lines[0].startswith("::warning")
    assert A in waiver.lines[0] and B in waiver.lines[0]
    result = apply_inherited_red(RED_TEST, waiver)
    assert result.ok
    assert not any(line.startswith("FAIL") for line in result.lines)
    assert any("WAIVED" in line for line in result.lines)


def test_one_new_failure_fails() -> None:
    waiver = decide_waiver(_evidence(pr_ids=frozenset({A, NEW})))
    assert not waiver.granted
    assert NEW in waiver.lines[0]
    assert not apply_inherited_red(RED_TEST, waiver).ok


def test_no_repair_open_fails() -> None:
    assert not decide_waiver(_evidence(repair_prs=())).granted


def test_diff_touching_a_failing_test_file_fails() -> None:
    waiver = decide_waiver(_evidence(changed_files=frozenset({"tests/ui/test_b.py"})))
    assert not waiver.granted
    assert "tests/ui/test_b.py" in waiver.lines[0]


def test_main_unreadable_fails_closed_with_a_warning() -> None:
    def unreadable() -> Evidence:
        raise Unmeasured("no completed ci.yml run on main")

    waiver = try_waiver(unreadable)
    assert not waiver.granted
    assert waiver.lines[0].startswith("::warning") and "failing closed" in waiver.lines[0]
    assert not apply_inherited_red(RED_TEST, waiver).ok


def test_red_shard_with_no_ids_or_non_pytest_ids_fails_closed() -> None:
    assert not decide_waiver(_evidence(pr_ids=frozenset())).granted
    node = "not ok - prefs golden"
    assert not decide_waiver(_evidence(pr_ids=frozenset({node}), main_ids=frozenset({node}))).granted


def test_only_the_test_job_is_ever_waivable() -> None:
    assert only_test_job_failed(RED_TEST.lines, "test")
    both = (*RED_TEST.lines, "FAIL frontend: failure (in scope, must succeed)")
    assert not only_test_job_failed(both, "test")
    cancelled = ("FAIL test: cancelled (in scope, must succeed)",)
    assert not only_test_job_failed(cancelled, "test")
    assert not only_test_job_failed(("ok   test: success",), "test")


def test_failing_test_file_reads_pytest_node_ids() -> None:
    assert failing_test_file(B) == "tests/ui/test_b.py"
    assert failing_test_file("ERROR tests/x.py") == "tests/x.py"
    assert failing_test_file("svelte-check src/a.svelte:3") is None


def test_a_refused_waiver_keeps_the_original_failure_lines() -> None:
    result = apply_inherited_red(RED_TEST, Waiver(False, ("inherited-red: no",)))
    assert not result.ok
    assert RED_TEST.lines[-1] in result.lines
