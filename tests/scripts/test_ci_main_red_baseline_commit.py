"""Tests for which COMMIT scripts/ci_main_red.py's baseline was measured at.

Split out of test_ci_main_red.py, which crossed the file size ceiling. The walk's identity
rules live there; what lives here is the separate question of whether the result may be
treated as current, which is what every staleness verdict downstream rests on.

Regression lines:
  - if the stamp names a run that measured nothing then broken
  - if a walk spanning two workflows reports only one of their commits then broken
  - if an older verdict that supplied a failure is left out of the contributing set then broken
  - if a verdict that only re-measured a decided name marks the baseline stale then broken
  - if a walk that measured nothing claims a contributing commit then broken

[if] the baseline is not the newest commit that measured [then] fail, [else stop].
"""

from __future__ import annotations

import pytest

from scripts.ci_main_red import (
    Job,
    Verdict,
    _walk_every_workflow,
    main_red_identities,
)

pytestmark = pytest.mark.requirement("OPS-16")


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


def test_an_older_verdict_that_supplies_a_failure_makes_the_baseline_stale():
    """Sol's second P1 on #3293. The newest verdict can sit on the pinned head and still not
    have measured everything: the window fills names it never ran from OLDER verdicts at
    other commits. Recording only the newest commit stamped such a baseline FRESH, so an
    identity that a trunk repair had already fixed could be subtracted from a pull request
    and let a genuine regression merge."""
    jobs = {
        1: [Job(11, "shard 1", "success")],
        2: [Job(21, "shard 2", "failure")],
    }
    walk = main_red_identities(
        [Verdict(1, 1, "n" * 40), Verdict(2, 1, "o" * 40)],
        lambda run_id, _attempt: jobs[run_id],
        lambda _job_id: frozenset({"FAILED tests/t.py::a"}),
    )
    assert walk.identities == {"FAILED tests/t.py::a"}, "the older verdict supplied it"
    assert walk.measured_sha == "n" * 40, "the newest is still the newest"
    assert walk.measured_shas == frozenset({"n" * 40, "o" * 40}), (
        "both commits contributed, so a baseline pinned at either one alone is stale"
    )


def test_a_verdict_that_only_re_measures_a_decided_name_does_not_make_it_stale():
    """The control, in the direction the fix above could overshoot. An older verdict that
    runs a name the newest one already DECIDED contributes nothing to the result, it only
    lets the window close. Counting it would report staleness a baseline does not have, and
    that is not a harmless bias: every stale baseline degrades its verdict to UNKNOWN, so
    over-reporting would retire the tool quietly rather than fail loudly."""
    jobs = {
        1: [Job(11, "shard 1", "failure")],
        2: [Job(21, "shard 1", "failure")],
    }
    walk = main_red_identities(
        [Verdict(1, 1, "n" * 40), Verdict(2, 1, "o" * 40)],
        lambda run_id, _attempt: jobs[run_id],
        lambda _job_id: frozenset({"FAILED tests/t.py::a"}),
    )
    assert walk.measured_shas == frozenset({"n" * 40}), (
        "the older verdict decided nothing the result rests on"
    )


def test_a_walk_that_measured_nothing_reports_no_contributing_commit():
    """The negative control. No verdict decided anything, so there is no commit to name, and
    the empty set must not be read as a baseline measured at the head."""
    walk = main_red_identities(
        [Verdict(1, 1, "n" * 40)],
        lambda _run_id, _attempt: [Job(11, "shard 1", "skipped")],
        lambda _job_id: frozenset(),
    )
    assert walk.measured_sha == ""
    assert walk.measured_shas == frozenset()
