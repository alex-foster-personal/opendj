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
    unmeasured_baseline_names,
    verdict_runs,
)
from scripts.review_gh import TriageError

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
    """The union is for a trunk that FLAPS: the same job red on two of the last three runs,
    with a different test named each time, is main's red and not the pull request's."""
    runs = {
        1: [Job(11, "shard 1", "failure")],
        2: [Job(21, "shard 1", "failure")],
        3: [Job(31, "shard 1", "success")],
    }
    ids = {11: frozenset({"FAILED tests/t.py::a"}), 21: frozenset({"FAILED tests/t.py::b"})}
    red, _ = _walk(runs, ids, [1, 2, 3])
    assert red.identities == {"FAILED tests/t.py::a", "FAILED tests/t.py::b"}


def test_a_failure_a_newer_verdict_ran_green_is_dropped_from_the_union():
    """Sol's P1 on #3293. This test previously asserted the opposite and so pinned the
    defect: the union retained an identity from two runs back although main's NEWEST run
    passed that same job, which is main's PAST, not main's present. A pull request
    reintroducing a test trunk repair had already fixed read KNOWN_RED and merged."""
    runs = {
        1: [Job(11, "shard 1", "success")],
        2: [Job(21, "shard 1", "failure")],
        3: [Job(31, "shard 1", "success")],
    }
    red, _ = _walk(runs, {21: frozenset({"FAILED tests/t.py::a"})}, [1, 2, 3])
    assert red.identities == frozenset()
    assert red.failed_job_names == frozenset()


def test_a_job_the_newer_verdicts_never_measured_still_reads_from_an_older_one():
    """The control for the overshoot. Dropping every older failure outright, rather than
    only the ones a newer verdict measured green, would delete the flap tolerance the
    window exists for and call main's standing red a GENUINE pull request failure."""
    runs = {
        1: [Job(11, "shard 1", "cancelled")],
        2: [Job(21, "shard 1", "failure")],
    }
    red, _ = _walk(runs, {21: frozenset({"FAILED tests/t.py::a"})}, [1, 2])
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


def test_an_always_empty_log_gives_up_after_three_reads_and_raises():
    """It raises rather than returning empty, so the caller can tell "no failing test named"
    apart from "never read"."""
    reads: list[int] = []

    def empty(job_id: int) -> str:
        reads.append(job_id)
        return ""

    with pytest.raises(LogUnreadable):
        job_log_identities(7, empty, lambda _s: None)
    assert len(reads) == 3


def test_a_job_failing_without_identities_on_main_is_unmeasured_not_a_red_job_name():
    """This test previously asserted the opposite, and the opposite was the bug.

    Issue #3276 is the frontend bundle budget failing on main with no test identity. Its log
    was READ and named nothing, so the job NAME is all that survives, and a name says main is
    red here, not what failed. Counted red, a pull request failing the same check reads
    MAIN_RED_JOB and merges: main 79 KB over budget and the pull request 300 KB over share
    one name and are not the same failure. Counted unmeasured, the watch exits UNKNOWN."""
    frontend = "frontend unit + check + build"
    runs = {
        1: [Job(11, frontend, "failure"), Job(12, "contracts", "success")],
        2: [Job(21, frontend, "failure"), Job(22, "contracts", "success")],
        3: [Job(31, frontend, "failure"), Job(32, "contracts", "success")],
    }
    red, _ = _walk(runs, {}, [1, 2, 3])
    assert red.failed_job_names == frozenset()
    assert red.unreadable_job_names == {frontend}


def test_a_job_whose_log_named_a_failing_test_is_still_measured():
    """The control, in the direction the fix could overshoot: a job main was measured on must
    stay measured, or the baseline empties out and every failure reads as a new one."""
    shard = "pytest fast lane (shard 1 of 5)"
    identity = "FAILED tests/a/test_main.py::test_red_on_main"
    runs = {1: [Job(11, shard, "failure")]}
    red, _ = _walk(runs, {11: frozenset({identity})}, [1])
    assert red.failed_job_names == {shard}
    assert red.unreadable_job_names == frozenset()
    assert identity in red.identities


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


def test_an_unreadable_job_does_not_become_a_trusted_red_job_name():
    """The name must NOT be retained. Retained, a pull request job with the same name and no
    identity classifies MAIN_RED_JOB and merges off a baseline that was never measured."""
    jobs = {1: [Job(11, "frontend unit + check + build", "failure")]}

    def unreadable(job_id: int) -> frozenset[str]:
        raise LogUnreadable(job_id)

    walk = main_red_identities(
        [Verdict(1, 1)], lambda run_id, attempt: jobs[run_id], unreadable, window=1
    )

    assert walk.failed_job_names == frozenset()
    assert walk.unreadable_job_names == frozenset({"frontend unit + check + build"})


def test_a_job_read_on_an_older_verdict_stops_being_unreadable():
    """The control: the walk still resolves it, and a resolved name is trusted again."""
    name = "frontend unit + check + build"
    jobs = {1: [Job(11, name, "failure")], 2: [Job(22, name, "failure")]}

    def identities_of(job_id: int) -> frozenset[str]:
        if job_id == 11:
            raise LogUnreadable(job_id)
        return frozenset({"FAILED tests/test_a.py::test_x"})

    walk = main_red_identities(
        [Verdict(1, 1), Verdict(2, 1)], lambda run_id, attempt: jobs[run_id], identities_of
    )

    assert name in walk.failed_job_names
    assert walk.unreadable_job_names == frozenset()
    assert "FAILED tests/test_a.py::test_x" in walk.identities


# ----- a failed check run at main's head is unmeasured, never red -----


def test_a_failed_check_run_name_is_carried_as_unmeasured_not_as_red():
    """Sol's P1 on #3293. A check run gives a conclusion and no log this tool parsed, so it
    says main is red under that name and NOT what failed. Counted red it reads MAIN_RED_JOB
    against a zero-identity pull request failure and merges off an unmeasured baseline."""
    assert unmeasured_baseline_names(
        frozenset(), frozenset({"other-lazy bundle budget"}), frozenset()
    ) == frozenset({"other-lazy bundle budget"})


def test_a_name_whose_log_was_actually_read_is_not_called_unmeasured():
    """The control, in the direction the fix could overshoot: a name main was measured on
    must stay measured, or every known-red job on main becomes UNKNOWN and nothing merges."""
    assert unmeasured_baseline_names(
        frozenset({"pytest fast lane (shard 1 of 5)"}),
        frozenset({"pytest fast lane (shard 1 of 5)"}),
        frozenset({"pytest fast lane (shard 1 of 5)"}),
    ) == frozenset()


def test_names_from_both_sources_are_unioned():
    assert unmeasured_baseline_names(
        frozenset({"unreadable job"}), frozenset({"failed check run"}), frozenset()
    ) == frozenset({"unreadable job", "failed check run"})


def test_a_refused_log_read_leaves_that_job_unmeasured_not_the_whole_baseline():
    """Hit live Wed 16 Sep 2026. GitHub expires job logs, so an older job in the baseline
    window answers 404. Propagated, one expired log killed the ENTIRE main-red measurement
    and the watcher had no baseline at all; the point of LogUnreadable is that exactly one
    job goes unmeasured."""
    def refuse(_job_id: int) -> str:
        raise TriageError("gh: HTTP 404")

    with pytest.raises(LogUnreadable):
        read_job_log(1, refuse, lambda _s: None)


def test_a_read_that_succeeds_after_a_refusal_is_still_returned():
    """The control: the retry must not be turned into a swallow. A transient refusal
    followed by a good read is a MEASURED job, and treating it as unreadable would push
    every flaky read into UNKNOWN and stop the watcher ever reporting a clean baseline."""
    calls: list[int] = []

    def flaky(job_id: int) -> str:
        calls.append(job_id)
        if len(calls) == 1:
            raise TriageError("gh: HTTP 502")
        return "FAILED tests/test_a.py::test_b"

    assert read_job_log(1, flaky, lambda _s: None) == "FAILED tests/test_a.py::test_b"
