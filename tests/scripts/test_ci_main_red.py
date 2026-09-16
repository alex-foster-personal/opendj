"""Tests for scripts/ci_main_red.py, main's red baseline for the fail-fast watcher.

Regression lines:
  - if an identity failing in any of the newest 3 verdicts is missing then broken
  - if a job the window left unmeasured is not read from the next older verdict then broken
  - if the walk reads past the first older verdict that measured the job then broken
  - if an older verdict's jobs the window DID measure leak into the baseline then broken
  - if a rerun still in progress is not counted as a verdict then broken
  - if an empty log read is taken for "this job failed no test" then broken
  - if a log that stays empty after every retry returns a value instead of raising then broken
  - if a job failing without identities on main's measured runs is not a red job name then broken
"""

from __future__ import annotations

import pytest

from scripts.ci_main_red import (
    Job,
    LogUnreadable,
    Verdict,
    job_log_identities,
    main_red_identities,
    read_job_log,
    verdict_runs,
)

pytestmark = pytest.mark.requirement("OPS-16")


def _walk(runs: dict[int, list[Job]], ids: dict[int, frozenset[str]], order: list[int]):
    reads: list[int] = []

    def jobs_of(run_id: int, attempt: int) -> list[Job]:
        reads.append(run_id)
        return runs[run_id]

    red = main_red_identities(
        [Verdict(r, 1) for r in order], jobs_of, lambda j: ids.get(j, frozenset())
    )
    return red, reads


def test_the_window_is_a_union_over_three_verdicts():
    runs = {
        1: [Job(11, "shard 1", "success")],
        2: [Job(21, "shard 1", "failure")],
        3: [Job(31, "shard 1", "success")],
    }
    red, _ = _walk(runs, {21: frozenset({"FAILED tests/t.py::a"})}, [1, 2, 3])
    assert red.identities == {"FAILED tests/t.py::a"}


def test_an_unmeasured_job_is_read_from_the_next_older_verdict_and_no_deeper():
    runs = {
        1: [Job(11, "shard 1", "cancelled"), Job(12, "shard 2", "success")],
        2: [Job(21, "shard 1", "failure"), Job(22, "shard 2", "success")],
        3: [Job(31, "shard 1", "failure"), Job(32, "shard 2", "success")],
        4: [Job(41, "shard 1", "failure"), Job(42, "shard 2", "failure")],
        5: [Job(51, "shard 1", "failure")],
    }
    ids = {
        41: frozenset({"FAILED tests/s1.py::old"}),
        42: frozenset({"FAILED tests/s2.py::measured_in_window"}),
        51: frozenset({"FAILED tests/s1.py::too_deep"}),
    }
    red, reads = _walk(runs, ids, [1, 2, 3, 4, 5])
    assert red.identities == {"FAILED tests/s1.py::old"}
    assert 5 not in reads


def test_a_rerun_in_progress_counts_as_a_verdict_and_a_plain_in_progress_run_does_not():
    runs = [
        {"id": 1, "status": "in_progress", "conclusion": None, "run_attempt": 2},
        {"id": 2, "status": "in_progress", "conclusion": None, "run_attempt": 1},
        {"id": 3, "status": "completed", "conclusion": "cancelled", "run_attempt": 1},
        {"id": 4, "status": "completed", "conclusion": "failure", "run_attempt": 1},
    ]
    assert verdict_runs(runs) == [Verdict(1, 2), Verdict(4, 1)]


def test_an_empty_log_read_is_retried_before_it_is_believed():
    """An empty read is gh failing, not a job with no failing test (measured Wed 16 Sep 2026:
    39 identities against the reference implementation's 118, same main SHA)."""
    reads: list[int] = []
    real = "2026-09-16T09:00:00Z FAILED tests/a/test_x.py::test_one\n"

    def flaky(job_id: int) -> str:
        reads.append(job_id)
        return "" if len(reads) < 3 else real

    assert job_log_identities(7, flaky, lambda _s: None) == {"FAILED tests/a/test_x.py::test_one"}
    assert len(reads) == 3


def test_an_always_empty_log_gives_up_after_three_reads():
    reads: list[int] = []

    def empty(job_id: int) -> str:
        reads.append(job_id)
        return ""

    assert job_log_identities(7, empty, lambda _s: None) == frozenset()
    assert len(reads) == 3


def test_a_job_failing_without_identities_on_main_is_a_red_job_name():
    """Issue #3276: the frontend bundle budget failed on main's measured runs with no test
    identity while main's newest run was still queued."""
    frontend = "frontend unit + check + build"
    runs = {
        1: [Job(11, frontend, "failure"), Job(12, "contracts", "success")],
        2: [Job(21, frontend, "failure"), Job(22, "contracts", "success")],
        3: [Job(31, frontend, "failure"), Job(32, "contracts", "success")],
    }
    red, _ = _walk(runs, {}, [1, 2, 3])
    assert red.failed_job_names == {frontend}


def test_a_log_that_stays_empty_after_every_retry_raises_rather_than_returning_empty():
    """An unreadable log is UNMEASURED. Returned as "", it reads as "this job named no
    failing test", which is how an unmeasured job earns a verdict it did not deserve."""
    reads: list[int] = []

    def never_readable(job_id: int) -> str:
        reads.append(job_id)
        return ""

    with pytest.raises(LogUnreadable):
        read_job_log(41, never_readable, lambda _: None)
    assert len(reads) == 3


def test_a_log_readable_on_a_later_try_is_returned():
    """The control: the retry must still succeed, or the raise is just a slower failure."""
    attempts = iter(["", "", "2026-09-16T09:00:00Z FAILED tests/test_a.py::test_x\n"])

    log = read_job_log(41, lambda _: next(attempts), lambda _: None)
    assert "test_x" in log


def test_the_baseline_treats_an_unreadable_log_as_no_identities_so_the_walk_continues():
    """`job_log_identities` absorbs it: an empty identity set is what tells the window walk
    the job is still unmeasured and must be read from an older verdict."""
    assert job_log_identities(41, lambda _: "", lambda _: None) == frozenset()
