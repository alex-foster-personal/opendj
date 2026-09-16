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

import json

import pytest

from scripts.ci_main_red import (
    Job,
    LogUnreadable,
    MainRed,
    Verdict,
    WalkResult,
    _walk_every_workflow,
    cached_main_red,
    fetch_main_red,
    job_log_identities,
    main_red_identities,
    read_job_log,
    unattributable_baseline,
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


def test_the_newest_measured_run_of_a_job_is_the_only_one_that_names_its_red():
    """Sol's P1 on #3293. The window was a plain UNION, so an older run's identity survived a
    newer run of the same job that did not name it. The newer run RAN that test and it passed,
    so a pull request reintroducing it is a regression, and subtracting it merged one."""
    runs = {
        1: [Job(11, "shard 1", "failure")],
        2: [Job(21, "shard 1", "failure")],
        3: [Job(31, "shard 1", "success")],
    }
    ids = {11: frozenset({"FAILED tests/t.py::a"}), 21: frozenset({"FAILED tests/t.py::b"})}
    red, _ = _walk(runs, ids, [1, 2, 3])
    assert red.identities == {"FAILED tests/t.py::a"}


def test_an_older_run_still_names_the_red_of_a_job_the_newest_run_never_measured():
    """The control against the overshoot: newest-measured-WINS, not newest-run-wins. The
    window exists for the job a newer run left unmeasured, and reading it there is the whole
    reason the walk goes deeper than one verdict at all."""
    runs = {
        1: [Job(11, "shard 1", "cancelled"), Job(12, "shard 2", "success")],
        2: [Job(21, "shard 1", "failure"), Job(22, "shard 2", "success")],
    }
    ids = {21: frozenset({"FAILED tests/t.py::a"})}
    red, _ = _walk(runs, ids, [1, 2])
    assert red.identities == {"FAILED tests/t.py::a"}
    assert red.failed_job_names == frozenset({"shard 1"})


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
    """Only a job NOTHING has measured keeps falling through. Once a verdict answers for a
    name -- green, or red with identities -- older verdicts are that name's past and are not
    read at all, which is also why the walk stops short of verdict 5 here."""
    runs = {
        1: [Job(11, "shard 1", "cancelled"), Job(12, "shard 2", "success")],
        2: [Job(21, "shard 1", "cancelled"), Job(22, "shard 2", "success")],
        3: [Job(31, "shard 1", "cancelled"), Job(32, "shard 2", "success")],
        4: [Job(41, "shard 1", "failure"), Job(42, "shard 2", "failure")],
        5: [Job(51, "shard 1", "failure")],
    }
    ids = {
        41: frozenset({"FAILED tests/s1.py::old"}),
        42: frozenset({"FAILED tests/s2.py::superseded_by_verdict_1"}),
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


def test_an_older_verdict_does_not_supply_a_cause_for_a_newer_unreadable_failure():
    """A newer run RAN this job and it failed; only the reason could not be read. An older
    run's identities are a guess at that reason, and the module already refuses that guess
    for the zero-identity case: two failures under one name share a name and nothing else.
    Terminal here, matching PR failures end UNKNOWN and a person looks."""
    name = "frontend unit + check + build"
    jobs = {1: [Job(11, name, "failure")], 2: [Job(22, name, "failure")]}

    def identities_of(job_id: int) -> frozenset[str]:
        if job_id == 11:
            raise LogUnreadable(job_id)
        return frozenset({"FAILED tests/test_a.py::test_x"})

    walk = main_red_identities(
        [Verdict(1, 1), Verdict(2, 1)], lambda run_id, attempt: jobs[run_id], identities_of
    )

    assert walk.unreadable_job_names == frozenset({name})
    assert walk.failed_job_names == frozenset()
    assert walk.identities == frozenset()


def test_an_older_verdict_still_answers_for_a_job_the_unreadable_run_never_ran():
    """The control: an unreadable log is terminal for the job it was READ for, not for the
    whole walk. A different job the newer verdict never ran is still read from the older one."""
    unreadable_name = "frontend unit + check + build"
    jobs = {
        1: [Job(11, unreadable_name, "failure")],
        2: [Job(22, unreadable_name, "failure"), Job(23, "shard 1", "failure")],
    }

    def identities_of(job_id: int) -> frozenset[str]:
        if job_id == 11:
            raise LogUnreadable(job_id)
        return frozenset({f"FAILED tests/test_{job_id}.py::test_x"})

    walk = main_red_identities(
        [Verdict(1, 1), Verdict(2, 1)], lambda run_id, attempt: jobs[run_id], identities_of
    )

    assert walk.unreadable_job_names == frozenset({unreadable_name})
    assert walk.failed_job_names == frozenset({"shard 1"})
    assert walk.identities == frozenset({"FAILED tests/test_23.py::test_x"})


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


def _walk_attempts(by_attempt: dict[int, list[Job]], ids: dict[int, frozenset[str]]):
    """One verdict whose job list differs per ATTEMPT, i.e. a run whose failed jobs were
    rerun."""
    return main_red_identities(
        [Verdict(1, max(by_attempt))],
        lambda _run_id, attempt: by_attempt[attempt],
        lambda job_id: ids.get(job_id, frozenset()),
    )


def test_a_rerun_attempt_that_succeeded_supersedes_the_attempt_it_reran():
    """Sol's P1 on #3293. Attempts were walked oldest first while the green set was applied
    only after the whole verdict, so attempt 1's identities survived attempt 2 rerunning the
    same job to success. Main had fixed it and a pull request reintroducing it read
    KNOWN_RED."""
    red = _walk_attempts(
        {1: [Job(11, "shard 1", "failure")], 2: [Job(12, "shard 1", "success")]},
        {11: frozenset({"FAILED tests/t.py::a"})},
    )
    assert red.identities == frozenset()
    assert red.failed_job_names == frozenset()


def test_a_rerun_that_failed_again_names_main_red_from_the_rerun_alone():
    """A rerun that failed AGAIN still names main's red -- the shard is not reported green
    between its own attempts -- but it names it from the NEWEST attempt. Attempt 2 ran test
    `a` and it passed there, so `a` is not something a pull request may be excused for.
    A shard flaky across its own attempts is what KNOWN_FLAKES is for, and reading GENUINE
    for a flake is loud, where subtracting a real regression is silent."""
    red = _walk_attempts(
        {1: [Job(11, "shard 1", "failure")], 2: [Job(12, "shard 1", "failure")]},
        {11: frozenset({"FAILED tests/t.py::a"}), 12: frozenset({"FAILED tests/t.py::b"})},
    )
    assert red.identities == {"FAILED tests/t.py::b"}
    assert red.failed_job_names == frozenset({"shard 1"})


def test_the_order_one_attempt_returns_its_jobs_in_cannot_change_the_answer():
    """The control on the within-attempt resolution: a name that succeeded anywhere in an
    attempt is decided green by that attempt, whichever order GitHub listed the jobs in."""
    both_ways = [
        [Job(11, "shard 1", "failure"), Job(12, "shard 1", "success")],
        [Job(12, "shard 1", "success"), Job(11, "shard 1", "failure")],
    ]
    for jobs in both_ways:
        red = main_red_identities(
            [Verdict(1, 1)],
            lambda _run_id, _attempt, jobs=jobs: jobs,
            lambda _job_id: frozenset({"FAILED tests/t.py::a"}),
        )
        assert red.identities == frozenset()
        assert red.failed_job_names == frozenset()


class _Walked(Exception):
    """Raised by the stub in place of the verdict walk, so a test can assert the cache was
    REJECTED without standing up a fake GitHub. Reaching the walk is the observable."""


def _raise_walked() -> MainRed:
    raise _Walked


def _write_cache(path, sha: str, identities: list[str]) -> None:
    path.write_text(
        json.dumps(
            {
                "identities": identities,
                "failed_job_names": [],
                "main_sha": sha,
                "unreadable_job_names": [],
                "measured_sha": sha,
                "measured_shas": [sha],
            }
        ),
        encoding="utf-8",
    )


def test_a_fresh_cache_about_an_older_main_is_not_reused(tmp_path):
    """Sol's P1 on #3293. Age was the whole test, and main moves inside the ten-minute TTL:
    trunk repair merges a fix and for the rest of that window a pull request reintroducing
    the same failure is still subtracted as main's red and exits mergeable."""
    cache = tmp_path / "main-red.json"
    _write_cache(cache, "a" * 40, ["FAILED tests/t.py::fixed_on_main"])
    with pytest.raises(_Walked):
        cached_main_red(
            cache,
            now=lambda: cache.stat().st_mtime,
            main_sha=lambda: "b" * 40,
            fetch=_raise_walked,
        )


def test_a_fresh_cache_about_the_current_main_is_reused(tmp_path):
    """The control: the cache exists because the verdict walk is the expensive call, and a
    check that rejected every cache would make the watcher re-walk main on every poll."""
    cache = tmp_path / "main-red.json"
    _write_cache(cache, "a" * 40, ["FAILED tests/t.py::still_red"])
    got = cached_main_red(
        cache, now=lambda: cache.stat().st_mtime, main_sha=lambda: "a" * 40, fetch=_raise_walked
    )
    assert got.identities == {"FAILED tests/t.py::still_red"}


# ----- the walk is pinned to one main head -----


def _pin_walk(heads: list[str], result: WalkResult | None = None, tries: int = 3):
    """`fetch_main_red` over a scripted sequence of main-SHA reads. The walk is a stub, so
    the only thing under test is whether the two reads bracketing it agreed."""
    seen = list(heads)
    walk = result or WalkResult(
        frozenset({"FAILED tests/t.py::a"}), frozenset({"shard 1"}), frozenset()
    )
    return fetch_main_red(
        main_sha=lambda: seen.pop(0),
        walk=lambda: walk,
        failed_check_runs=lambda _sha: frozenset(),
        tries=tries,
    )


def test_a_walk_whose_head_held_still_is_stamped_with_that_head():
    red = _pin_walk(["aaa", "aaa"])
    assert red.main_sha == "aaa"
    assert red.identities == {"FAILED tests/t.py::a"}


def test_a_head_that_moved_under_the_walk_is_retried_before_it_is_believed():
    """The retry is the cheap half of the fix: main merges in bursts, so one collision is
    ordinary and a second walk usually lands inside one head."""
    red = _pin_walk(["aaa", "bbb", "bbb", "bbb"])
    assert red.main_sha == "bbb"
    assert red.identities == {"FAILED tests/t.py::a"}


def test_a_head_that_moves_under_every_walk_subtracts_nothing():
    """Sol's P1 on #3293. The head used to be read AFTER the walk, so failures measured at
    the old head were stamped with the new one and served from cache for its whole TTL: a
    pull request reintroducing a failure trunk repair had just fixed read KNOWN_RED. With no
    head the whole walk saw, there is no baseline, and an empty baseline is loud."""
    red = _pin_walk(["a", "b", "c", "d", "e", "f", "zzz"])
    assert red.main_sha == "zzz"
    assert red.identities == frozenset()
    assert red.failed_job_names == frozenset()


def test_an_unattributable_baseline_still_reports_mains_failing_names_as_unmeasured():
    """The control against the overshoot: subtracting nothing must not also mean CLAIMING
    nothing. A zero-identity pull request failure under a name main is red on still ends
    UNKNOWN rather than GENUINE, because nothing compared the two."""
    red = unattributable_baseline("zzz", frozenset({"frontend bundle budget"}))
    assert red.unreadable_job_names == frozenset({"frontend bundle budget"})
    assert red.identities == frozenset()


def test_a_cache_that_cannot_say_which_commit_it_measured_is_re_walked(tmp_path):
    """Sol's P1 on #3293, the upgrade half. A baseline written before the walk recorded its
    commit looks exactly like a current one, and reusing it serves identities under a head
    nothing checked them against. The missing key is the version gate."""
    cache = tmp_path / "main-red.json"
    cache.write_text(
        json.dumps(
            {
                "identities": ["FAILED tests/t.py::old"],
                "failed_job_names": [],
                "main_sha": "a" * 40,
                "unreadable_job_names": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(_Walked):
        cached_main_red(
            cache,
            now=lambda: cache.stat().st_mtime,
            main_sha=lambda: "a" * 40,
            fetch=_raise_walked,
        )


# ----- a baseline says which commit it is about -----


def test_the_walk_reports_the_commit_its_measurements_came_from():
    """The baseline is a claim about what it READ. Main's head usually has no completed run
    -- 8 of its newest 60 ci.yml runs were a completed verdict, 49 cancelled by the tip-only
    sweeper -- so the walk normally measures an older commit, and stamping the head over it
    is how a failure trunk repair had already fixed read as main's current red."""
    jobs = {
        1: [Job(11, "shard 1", "success")],
        2: [Job(21, "shard 1", "failure")],
    }
    walk = main_red_identities(
        [Verdict(1, 1, "n" * 40), Verdict(2, 1, "o" * 40)],
        lambda run_id, _attempt: jobs[run_id],
        lambda _job_id: frozenset({"FAILED tests/t.py::a"}),
    )
    assert walk.measured_sha == "n" * 40, "the newest verdict that measured anything"


def test_a_verdict_that_measured_nothing_does_not_claim_the_baselines_commit():
    """The control: the stamp names the commit that produced a MEASUREMENT, not whichever
    run happened to be newest. A verdict whose jobs were all skipped measured nothing, and
    naming it would put the baseline at a commit no identity in it came from."""
    jobs = {
        1: [Job(11, "shard 1", "skipped")],
        2: [Job(21, "shard 1", "failure")],
    }
    walk = main_red_identities(
        [Verdict(1, 1, "n" * 40), Verdict(2, 1, "o" * 40)],
        lambda run_id, _attempt: jobs[run_id],
        lambda _job_id: frozenset({"FAILED tests/t.py::a"}),
    )
    assert walk.measured_sha == "o" * 40
    assert walk.identities == {"FAILED tests/t.py::a"}


def _two_workflow_runs(first_sha: str, second_sha: str):
    """One completed run per workflow file, each measuring at its own commit."""
    per_workflow = {
        "ci.yml": [
            {"id": 1, "status": "completed", "conclusion": "failure", "run_attempt": 1,
             "head_sha": first_sha},
        ],
        "e2e.yml": [
            {"id": 2, "status": "completed", "conclusion": "failure", "run_attempt": 1,
             "head_sha": second_sha},
        ],
    }
    jobs = {
        1: [Job(11, "pytest fast lane (shard 1 of 5)", "failure")],
        2: [Job(21, "e2e gate", "failure")],
    }
    ids = {11: frozenset({"FAILED tests/a/test_a.py::test_one"}),
           21: frozenset({"FAILED tests/e/test_e.py::test_two"})}
    return (
        lambda workflow: per_workflow[workflow],
        lambda run_id, _attempt: jobs[run_id],
        lambda job_id: ids[job_id],
    )


def test_the_walk_records_the_commit_every_workflow_measured() -> None:
    """Sol's P1 on #3293. `ci.yml` and `e2e.yml` do not finish on the same commit, and the
    walk kept only the FIRST non-empty sha. A baseline whose ci half was measured at the
    pinned head then carried an e2e half measured at an older one while being stamped
    current, so that half's obsolete failures were subtracted as main's known red."""
    runs_of, jobs_of, identities_of = _two_workflow_runs("a" * 40, "b" * 40)
    walk = _walk_every_workflow(jobs_of, identities_of, runs_of)
    assert walk.measured_shas == frozenset({"a" * 40, "b" * 40})


def test_the_walk_still_reports_one_sha_when_the_workflows_agree() -> None:
    """The opposite direction: recording every sha must not manufacture a disagreement when
    both workflows really did measure the same commit, or every baseline reads stale and the
    watcher never subtracts anything again."""
    runs_of, jobs_of, identities_of = _two_workflow_runs("a" * 40, "a" * 40)
    walk = _walk_every_workflow(jobs_of, identities_of, runs_of)
    assert walk.measured_shas == frozenset({"a" * 40})
    assert walk.measured_sha == "a" * 40


def test_the_walk_collects_identities_from_both_workflows() -> None:
    """The presence half. The two assertions above are about SHAs, and both would pass if the
    second workflow were skipped entirely and contributed neither sha nor failure."""
    runs_of, jobs_of, identities_of = _two_workflow_runs("a" * 40, "b" * 40)
    walk = _walk_every_workflow(jobs_of, identities_of, runs_of)
    assert walk.identities == frozenset(
        {"FAILED tests/a/test_a.py::test_one", "FAILED tests/e/test_e.py::test_two"}
    )
