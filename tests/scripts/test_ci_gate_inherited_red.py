"""`ci gate` passes a pull request whose only failing tests are main's, under strict guards.

Regression lines:
  - if every failure is inherited and a repair is open but the gate stays red then broken
  - if one failure is not on main and the gate passes then broken
  - if no ci:trunk-repair pull request is open and the gate passes then broken
  - if the diff touches a failing test's file and the gate passes then broken
  - if main's red cannot be read and the gate passes then broken (must fail closed)
  - if a waived pass prints no ::warning:: naming the inherited ids then broken
  - if a failure in any job other than `test` is waived then broken
  - if an inherited test hides additional shard failure evidence then broken
"""

from __future__ import annotations

import pytest

from scripts import ci_gate_inherited_red as inherited_red
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


# Synthesized runner logs exercise the real shared parser and shard collector.
CLEAN_LOG = f"##[group]Run pytest\n##[endgroup]\n{A}\nProcess completed with exit code 1\n"
SHARD_NAME = f"{inherited_red.TEST_JOB_PREFIX} (1)"


def _github_shards(
    monkeypatch: pytest.MonkeyPatch,
    jobs: list[dict[str, object]],
    logs: dict[int, str],
) -> list[int]:
    requested: list[int] = []

    def api(path: str, token: str, raw: bool = False) -> object:
        assert token == "test-token"
        if path == "repos/o/r/actions/runs/10/attempts/2/jobs?per_page=100":
            assert not raw
            return {"jobs": jobs}
        for job_id, log in logs.items():
            if path == f"repos/o/r/actions/jobs/{job_id}/logs":
                assert raw
                requested.append(job_id)
                return log
        raise AssertionError(f"unexpected GitHub API request: {path}")

    monkeypatch.setattr(inherited_red, "_api", api)
    return requested


def _collect_shard_evidence() -> Evidence:
    return _evidence(pr_ids=inherited_red._red_shard_identities("o/r", "10", "2", "test-token"))


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ("Process completed with exit code 3", "exit code 3"),
        ("Process completed with exit code 137", "exit code 137"),
        ("##[error]The operation was canceled.", "The operation was canceled"),
        ("##[error]The job exceeded the maximum execution time.", "exceeded the maximum execution time"),
        ("OSError: No space left on device", "No space left on device"),
        (
            "##[group]Run upload-results\n##[endgroup]\nProcess completed with exit code 1",
            "exit code 1 in a step that named no failing test: upload-results",
        ),
    ],
)
def test_unexplained_shard_failure_refuses_waiver(monkeypatch: pytest.MonkeyPatch, extra: str, expected: str) -> None:
    _github_shards(
        monkeypatch,
        [{"name": SHARD_NAME, "id": 101, "conclusion": "failure"}],
        {101: CLEAN_LOG + extra + "\n"},
    )
    with pytest.raises(Unmeasured) as raised:
        _collect_shard_evidence()
    message = str(raised.value)
    assert SHARD_NAME in message and "job 101" in message and expected in message

    waiver = try_waiver(_collect_shard_evidence)
    assert not waiver.granted
    assert waiver.lines[0].startswith("::warning") and "failing closed" in waiver.lines[0]
    assert expected in waiver.lines[0]
    result = apply_inherited_red(RED_TEST, waiver)
    assert not result.ok
    assert RED_TEST.lines[-1] in result.lines


def test_test_owned_exit_one_remains_waivable(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _github_shards(
        monkeypatch,
        [{"name": SHARD_NAME, "id": 101, "conclusion": "failure"}],
        {101: CLEAN_LOG},
    )
    evidence = _collect_shard_evidence()
    assert evidence.pr_ids == frozenset({A})
    assert requested == [101]
    waiver = try_waiver(lambda: evidence)
    assert waiver.granted
    assert waiver.lines[0].startswith("::warning") and A in waiver.lines[0]
    assert apply_inherited_red(RED_TEST, waiver).ok


@pytest.mark.parametrize("contaminated_first", [False, True])
def test_one_contaminated_shard_invalidates_collection(
    monkeypatch: pytest.MonkeyPatch, contaminated_first: bool
) -> None:
    jobs = [
        {"name": SHARD_NAME, "id": 101, "conclusion": "failure"},
        {"name": f"{inherited_red.TEST_JOB_PREFIX} (2)", "id": 102, "conclusion": "failure"},
    ]
    if contaminated_first:
        jobs.reverse()
    _github_shards(monkeypatch, jobs, {101: CLEAN_LOG, 102: CLEAN_LOG + "Process completed with exit code 3\n"})
    waiver = try_waiver(_collect_shard_evidence)
    assert not waiver.granted
    assert "job 102" in waiver.lines[0]


def test_contaminated_main_shard_is_unmeasured(monkeypatch: pytest.MonkeyPatch) -> None:
    _github_shards(
        monkeypatch,
        [{"name": SHARD_NAME, "id": 101, "conclusion": "failure"}],
        {101: CLEAN_LOG + "Process completed with exit code 3\n"},
    )
    with pytest.raises(Unmeasured, match="exit code 3"):
        inherited_red._main_ids_at("o/r", {"id": 10, "run_attempt": 2}, "test-token")


def test_red_shard_without_identities_is_unmeasured(monkeypatch: pytest.MonkeyPatch) -> None:
    _github_shards(
        monkeypatch,
        [{"name": SHARD_NAME, "id": 101, "conclusion": "failure"}],
        {101: "pytest stopped before reporting any test\n"},
    )
    with pytest.raises(Unmeasured, match="names no failing test"):
        _collect_shard_evidence()


def test_nonfailing_and_nonpytest_jobs_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [
        {"name": SHARD_NAME, "id": index, "conclusion": conclusion}
        for index, conclusion in enumerate(("success", "cancelled", "skipped", None))
    ]
    jobs.append({"name": "frontend", "id": 100, "conclusion": "failure"})
    requested = _github_shards(monkeypatch, jobs, {})
    assert _collect_shard_evidence().pr_ids == frozenset()
    assert requested == []
