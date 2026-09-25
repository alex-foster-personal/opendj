"""Tests for scripts/ci_watch.py, the fail-fast PR watcher (SMARTEST-CI round 3).

Regression lines:
  - if a GENUINE failure does not stop the poll on the snapshot it appears in then broken
  - if a failure also on main stops the poll then broken
  - if a finished failure is reported twice then broken
  - if a cancelled or infra-killed job lets the watch end GREEN or KNOWN_RED_ONLY then broken
  - if a clean finish with no baseline reads GREEN then broken
  - if a head move, timeout or missing baseline reads anything but UNKNOWN then broken
  - if zero check runs ever end the poll as a success then broken
  - if an uncaught crash exits 1 (GENUINE) instead of 3 then broken
  - if a zero-identity failure prints no reason then broken

[if] the watcher calls a run decided without reading its checks [then] fail, [else stop].
"""

from __future__ import annotations

import inspect

import pytest

from scripts import ci_watch
from scripts.ci_failure_ids import JobVerdict
from scripts.ci_main_red import LogUnreadable, MainRed
from scripts.ci_wait_core import WaitStatus, poll_until_terminal
from scripts.ci_watch import Exit, FailureWatch, exit_for
from scripts.review_gh import TriageError

pytestmark = pytest.mark.requirement("OPS-16")

MAIN_FAIL = "FAILED tests/a/test_main.py::test_red_on_main"
NEW_FAIL = "FAILED tests/b/test_new.py::test_this_pr_broke"


def _check(check_id: int, name: str, status: str = "completed", conclusion: str | None = "failure"):
    return {
        "id": check_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "started_at": "2026-09-16T09:00:00Z",
        "html_url": f"https://github.com/o/r/actions/runs/1/job/{check_id}",
    }


def _watch(identities: dict[int, frozenset[str]], lines: list[str]) -> FailureWatch:
    def log_of(job_id: int) -> str:
        return "".join(f"2026-09-16T09:00:00Z {i}\n" for i in sorted(identities.get(job_id, ())))

    return FailureWatch(
        log_of=log_of,
        main_red=lambda: MainRed(frozenset({MAIN_FAIL}), frozenset(), "m" * 40),
        emit=lines.append,
        clock=lambda: 0.0,
    )


def _snapshot(*checks: dict) -> dict[str, dict]:
    return {check["name"]: check for check in checks}


def test_a_genuine_failure_stops_on_the_snapshot_it_appears_in():
    lines: list[str] = []
    watch = _watch({7: frozenset({NEW_FAIL})}, lines)
    outcome = watch.inspect(
        _snapshot(_check(7, "pytest shard 1"), _check(8, "e2e", "in_progress", None))
    )
    assert outcome is not None and outcome.status is WaitStatus.GENUINE_FAILURE
    assert any(NEW_FAIL in line for line in lines)


def test_a_failure_on_main_keeps_waiting_and_is_reported_once():
    lines: list[str] = []
    watch = _watch({7: frozenset({MAIN_FAIL})}, lines)
    snapshot = _snapshot(_check(7, "pytest shard 1"), _check(8, "e2e", "in_progress", None))
    assert watch.inspect(snapshot) is None
    assert watch.inspect(snapshot) is None
    assert sum("KNOWN_RED pytest shard 1" in line for line in lines) == 1


def test_a_cancelled_job_ends_unknown_not_green_or_known_red():
    watch = _watch({7: frozenset({MAIN_FAIL})}, [])
    watch.inspect(_snapshot(_check(7, "pytest shard 1"), _check(9, "e2e", conclusion="cancelled")))
    assert exit_for(WaitStatus.FAILURE, watch, has_baseline=True)[0] is Exit.UNKNOWN


def test_an_infra_killed_shard_ends_unknown():
    watch = _watch({}, [])
    watch.inspect(_snapshot(_check(7, "pytest fast lane (shard 2 of 5)")))
    assert exit_for(WaitStatus.FAILURE, watch, has_baseline=True)[0] is Exit.UNKNOWN


def test_only_main_red_failures_end_known_red_only():
    watch = _watch({7: frozenset({MAIN_FAIL})}, [])
    watch.inspect(_snapshot(_check(7, "pytest shard 1")))
    assert exit_for(WaitStatus.FAILURE, watch, has_baseline=True)[0] is Exit.KNOWN_RED_ONLY


def test_a_clean_finish_is_green_only_with_a_baseline():
    watch = _watch({}, [])
    assert exit_for(WaitStatus.SUCCESS, watch, has_baseline=True)[0] is Exit.GREEN
    assert exit_for(WaitStatus.SUCCESS, watch, has_baseline=False)[0] is Exit.UNKNOWN


@pytest.mark.parametrize(
    "status", [WaitStatus.TIMEOUT, WaitStatus.NO_BASELINE, WaitStatus.HEAD_MOVED]
)
def test_unmeasured_ends_are_unknown(status):
    assert exit_for(status, _watch({}, []), has_baseline=True)[0] is Exit.UNKNOWN


def test_zero_check_runs_never_end_the_poll_as_success():
    ticks = iter(range(100))
    result = poll_until_terminal(
        frozenset(),
        list,
        timeout_s=5,
        poll_interval_s=1,
        sleep=lambda _: None,
        clock=lambda: float(next(ticks)),
    )
    assert result[0] is WaitStatus.TIMEOUT


def test_no_check_runs_past_the_grace_period_stops_as_timeout():
    watch = _watch({}, [])
    now = iter([0.0, ci_watch.NO_RUNS_GRACE_S + 1])
    watch.clock = lambda: next(now)
    assert watch.inspect({}) is None
    outcome = watch.inspect({})
    assert outcome is not None and outcome.status is WaitStatus.TIMEOUT


def test_a_crash_exits_unknown_not_genuine():
    """An uncaught crash exits 1, and 1 is this tool's GENUINE code, so a watcher that
    fell over would read as a proven failure. The error injected is the real one: GitHub
    answering without `check_runs` is what `_check_runs_at_sha` raises KeyError on."""

    def raise_as_github_would(*_args, **_kwargs):
        raise KeyError("check_runs")

    assert ci_watch.main(["3288"], watch=raise_as_github_would) == Exit.UNKNOWN


def test_the_seam_defaults_to_the_real_watcher():
    """The control: a seam nothing uses by default is a second implementation. `main` must
    call the production `watch_pr` when the caller names nothing."""
    assert inspect.signature(ci_watch.main).parameters["watch"].default is ci_watch.watch_pr


def test_a_zero_identity_failure_prints_the_log_error_lines():
    lines: list[str] = []
    log = (
        '2026-09-16T09:44:38Z \x1b[36;1mecho "[ERROR] echoed-command-not-a-result"\x1b[0m\n'
        '2026-09-16T09:44:38Z [ERROR] budget "other-lazy" exceeded: 199450 bytes gzip\n'
        "2026-09-16T09:44:38Z ##[error]Process completed with exit code 1.\n"
    )
    watch = FailureWatch(
        log_of=lambda _job: log,
        main_red=lambda: MainRed(frozenset(), frozenset(), "m" * 40),
        emit=lines.append,
        clock=lambda: 0.0,
    )
    outcome = watch.inspect(_snapshot(_check(7, "frontend unit + check + build")))
    assert outcome is not None and outcome.status is WaitStatus.GENUINE_FAILURE
    blob = "\n".join(lines)
    assert 'budget "other-lazy" exceeded' in blob
    assert "echoed-command-not-a-result" not in blob


def test_a_check_whose_log_cannot_be_read_is_unmeasured_not_a_verdict():
    """A failed job the watcher could not read must end UNKNOWN (exit 3). Classified from an
    empty log it would look like a job that named no failing test, and a job name matching an
    infra or ratchet rule would then be waved through."""
    lines: list[str] = []

    def unreadable(job_id: int) -> str:
        raise LogUnreadable(job_id)

    watch = FailureWatch(
        log_of=unreadable,
        main_red=lambda: MainRed(frozenset({MAIN_FAIL}), frozenset(), "m" * 40),
        emit=lines.append,
        clock=lambda: 0.0,
    )
    outcome = watch.inspect(_snapshot(_check(7, "pytest fast lane (shard 2 of 5)")))

    assert outcome is None
    assert watch.verdicts[7] is None
    code, _ = exit_for(WaitStatus.FAILURE, watch, has_baseline=True)
    assert code is Exit.UNKNOWN
    assert any("log unreadable" in line for line in lines)


def test_an_unmeasured_baseline_job_ends_unknown_not_mergeable():
    """End to end: a check whose baseline counterpart was never read must not let the watch
    finish KNOWN_RED_ONLY, which the merging agent treats as mergeable."""
    name = "frontend unit + check + build"
    lines: list[str] = []
    watch = FailureWatch(
        log_of=lambda job_id: "",
        main_red=lambda: MainRed(
            frozenset({MAIN_FAIL}), frozenset(), "m" * 40, frozenset({name})
        ),
        emit=lines.append,
        clock=lambda: 0.0,
    )
    watch.inspect(_snapshot(_check(9, name)))

    code, _ = exit_for(WaitStatus.FAILURE, watch, has_baseline=True)
    assert code is Exit.UNKNOWN


def test_a_known_red_job_that_was_also_killed_is_unmeasured_not_known_red():
    """The wiring, not just the rule. A shard can fail a test main already fails AND be
    killed for a cap or a timeout. Passing beyond_tests=False here reads the kill as
    known-red debt and the agent merges on the strength of tests that were red anyway;
    the mutation that always passes False left every other test in this module green."""
    lines: list[str] = []
    log = (
        f"2026-09-16T09:44:38Z {MAIN_FAIL}\n"
        "2026-09-16T09:44:38Z ##[error]Process completed with exit code 137.\n"
    )
    watch = FailureWatch(
        log_of=lambda _job: log,
        main_red=lambda: MainRed(frozenset({MAIN_FAIL}), frozenset(), "m" * 40),
        emit=lines.append,
        clock=lambda: 0.0,
    )
    watch.inspect(_snapshot(_check(7, "pytest fast lane (shard 1 of 5)")))
    assert watch.verdicts[7].verdict is JobVerdict.UNEXPLAINED
    assert "exit code 137" in "\n".join(lines)


def test_a_known_red_job_that_finished_normally_is_still_known_red():
    """The control in the other direction: without it the fix could make every known-red
    job unmeasured, and the watcher could never report a pull request as clean again.

    The step echo is not decoration. A real runner log opens every step with it -- counted
    on job 104829711771, 12 steps, the exit-1 falling inside the one that ran pytest -- and
    this fixture predates the step partitioner, so without it the log models a shape GitHub
    does not emit and the control would fail for a reason the control is not about."""
    lines: list[str] = []
    log = (
        "2026-09-16T09:44:38Z ##[group]Run uv run pytest\n"
        f"2026-09-16T09:44:38Z {MAIN_FAIL}\n"
        "2026-09-16T09:44:38Z ##[error]Process completed with exit code 1.\n"
    )
    watch = FailureWatch(
        log_of=lambda _job: log,
        main_red=lambda: MainRed(frozenset({MAIN_FAIL}), frozenset(), "m" * 40),
        emit=lines.append,
        clock=lambda: 0.0,
    )
    watch.inspect(_snapshot(_check(7, "pytest fast lane (shard 1 of 5)")))
    assert watch.verdicts[7].verdict is JobVerdict.KNOWN_RED


def test_a_timed_out_job_is_unmeasured_even_when_its_log_reads_known_red():
    """A timed-out job's log stops wherever the clock did, so the runner's own terminal line
    may never have been written. Matching on log text alone reads a truncated log full of
    known-red identities as KNOWN_RED and exits mergeable despite the timeout. The
    CONCLUSION is evidence; it does not need the log's permission."""
    lines: list[str] = []
    log = f"2026-09-16T09:44:38Z {MAIN_FAIL}\n"
    watch = FailureWatch(
        log_of=lambda _job: log,
        main_red=lambda: MainRed(frozenset({MAIN_FAIL}), frozenset(), "m" * 40),
        emit=lines.append,
        clock=lambda: 0.0,
    )
    watch.inspect(
        _snapshot(_check(7, "pytest fast lane (shard 1 of 5)", conclusion="timed_out"))
    )
    assert watch.verdicts[7].verdict is JobVerdict.UNEXPLAINED
    assert "timed_out" in "\n".join(lines)


def test_a_plain_failure_with_the_same_log_is_still_known_red():
    """The control: without it the fix reads every conclusion as unexplained and the watcher
    can never report a pull request clean again."""
    lines: list[str] = []
    log = f"2026-09-16T09:44:38Z {MAIN_FAIL}\n"
    watch = FailureWatch(
        log_of=lambda _job: log,
        main_red=lambda: MainRed(frozenset({MAIN_FAIL}), frozenset(), "m" * 40),
        emit=lines.append,
        clock=lambda: 0.0,
    )
    watch.inspect(_snapshot(_check(7, "pytest fast lane (shard 1 of 5)")))
    assert watch.verdicts[7].verdict is JobVerdict.KNOWN_RED


def test_a_zero_identity_job_matching_a_main_red_job_name_ends_unknown_not_known_red():
    """The wiring, not the rule. Mutating classify_job's new BASELINE_MISMATCH verdict out of
    `exit_for`'s unmeasured bucket left the whole suite green while the job fell through to
    KNOWN_RED_ONLY and merged -- which is Sol's P1 restored with the fix still in place."""
    name = "frontend unit + check + build"
    watch = FailureWatch(
        log_of=lambda _job_id: "2026-09-16T09:00:00Z something failed, no test named\n",
        main_red=lambda: MainRed(frozenset({MAIN_FAIL}), frozenset({name}), "m" * 40),
        emit=lambda _line: None,
        clock=lambda: 0.0,
    )
    watch.inspect(_snapshot(_check(7, name)))
    assert watch.verdicts[7].verdict is JobVerdict.BASELINE_MISMATCH
    assert exit_for(WaitStatus.FAILURE, watch, has_baseline=True)[0] is Exit.UNKNOWN


def test_an_exit_the_log_cannot_attribute_is_unmeasured_not_known_red():
    """Sol's P1 on #3293, and the WIRING rather than the rule: the tri-state exists in
    `ci_failure_ids`, and the watcher has to actually ask for it. A log carrying known-red
    identities and an exit 1 with no step echoes cannot say the tests spent that exit, and
    reading the unknown as "they did" merged a job on a measurement nobody made."""
    lines: list[str] = []
    log = (
        f"2026-09-16T09:44:38Z {MAIN_FAIL}\n"
        "2026-09-16T09:44:38Z ##[error]Process completed with exit code 1.\n"
    )
    watch = FailureWatch(
        log_of=lambda _job: log,
        main_red=lambda: MainRed(frozenset({MAIN_FAIL}), frozenset(), "m" * 40),
        emit=lines.append,
        clock=lambda: 0.0,
    )
    watch.inspect(_snapshot(_check(7, "pytest fast lane (shard 1 of 5)")))
    assert watch.verdicts[7].verdict is JobVerdict.UNEXPLAINED
    assert "could not be partitioned" in "\n".join(lines)


def test_a_baseline_the_pull_request_advanced_past_ends_the_watch_unknown_not_known_red_only():
    """The WIRING, which the rule test cannot reach. A verdict that is unmergeable in
    `classify_job` and absent from `exit_for`'s unmeasured tuple is a rule nobody applies --
    that exact gap was found by mutation on this module earlier and is why this exists.

    Issue #3344: staleness is now about the PULL REQUEST's own merge base, not main's live
    tip -- `advanced_past` stands in for `git merge-base --is-ancestor measured_sha
    pr_head_sha`, and answering True here is what used to require comparing against main_sha."""
    lines: list[str] = []
    log = (
        "2026-09-16T09:44:38Z ##[group]Run uv run pytest\n"
        f"2026-09-16T09:44:38Z {MAIN_FAIL}\n"
        "2026-09-16T09:44:38Z ##[error]Process completed with exit code 1.\n"
    )
    watch = FailureWatch(
        log_of=lambda _job: log,
        main_red=lambda: MainRed(
            frozenset({MAIN_FAIL}),
            frozenset(),
            "h" * 40,
            frozenset(),
            "o" * 40,
            frozenset({"o" * 40}),
            {"o" * 40: frozenset({MAIN_FAIL})},
        ),
        emit=lines.append,
        clock=lambda: 0.0,
        pr_head_sha="p" * 40,
        advanced_past=lambda _measured, _pr: True,
    )
    watch.inspect(_snapshot(_check(7, "pytest fast lane (shard 1 of 5)")))
    assert watch.verdicts[7].verdict is JobVerdict.BASELINE_STALE
    code, why = exit_for(WaitStatus.FAILURE, watch, has_baseline=True)
    assert code is Exit.UNKNOWN, why


def test_only_the_commit_the_pull_request_advanced_past_makes_its_own_identities_stale():
    """Sol's P1 on #3293, narrowed by issue #3344: a walk spans several workflow files that
    finish on different commits, but staleness is bounded PER SOURCE COMMIT now, not
    wholesale. This job's own identity came from the commit the pull request advanced past,
    so it reads stale even though the baseline also holds an unrelated identity from a
    commit the pull request has NOT advanced past -- the old wholesale rule downgraded both,
    which is exactly the cost that blocked ~87% of polls and motivated this bound."""
    lines: list[str] = []
    log = (
        "2026-09-16T09:44:38Z ##[group]Run uv run pytest\n"
        f"2026-09-16T09:44:38Z {MAIN_FAIL}\n"
        "2026-09-16T09:44:38Z ##[error]Process completed with exit code 1.\n"
    )
    other = "FAILED tests/b/test_other.py::test_still_fresh"
    watch = FailureWatch(
        log_of=lambda _job: log,
        main_red=lambda: MainRed(
            frozenset({MAIN_FAIL, other}),
            frozenset(),
            "h" * 40,
            frozenset(),
            "h" * 40,
            frozenset({"h" * 40, "o" * 40}),
            {"h" * 40: frozenset({other}), "o" * 40: frozenset({MAIN_FAIL})},
        ),
        emit=lines.append,
        clock=lambda: 0.0,
        pr_head_sha="p" * 40,
        advanced_past=lambda measured, _pr: measured == "o" * 40,
    )
    watch.inspect(_snapshot(_check(7, "pytest fast lane (shard 1 of 5)")))
    assert watch.verdicts[7].verdict is JobVerdict.BASELINE_STALE


def test_a_baseline_the_pull_request_has_not_advanced_past_is_not_stale():
    """The opposite direction. A pull request whose merge base predates every measurement in
    the baseline must still read its known-red failures as KNOWN_RED, or the watcher becomes
    useless the moment ANY per-commit baseline exists."""
    lines: list[str] = []
    log = (
        "2026-09-16T09:44:38Z ##[group]Run uv run pytest\n"
        f"2026-09-16T09:44:38Z {MAIN_FAIL}\n"
        "2026-09-16T09:44:38Z ##[error]Process completed with exit code 1.\n"
    )
    watch = FailureWatch(
        log_of=lambda _job: log,
        main_red=lambda: MainRed(
            frozenset({MAIN_FAIL}),
            frozenset(),
            "h" * 40,
            frozenset(),
            "h" * 40,
            frozenset({"h" * 40}),
            {"h" * 40: frozenset({MAIN_FAIL})},
        ),
        emit=lines.append,
        clock=lambda: 0.0,
        pr_head_sha="p" * 40,
        advanced_past=lambda _measured, _pr: False,
    )
    watch.inspect(_snapshot(_check(7, "pytest fast lane (shard 1 of 5)")))
    assert watch.verdicts[7].verdict is JobVerdict.KNOWN_RED


# ----- the real advanced_past seam (issue #3344) -----


def test_advanced_past_reports_true_when_the_pull_request_contains_the_measured_commit(
    monkeypatch,
):
    monkeypatch.setattr(ci_watch, "fetch_commits", lambda root, *shas: None)
    monkeypatch.setattr(ci_watch, "is_ancestor", lambda root, ancestor, descendant: True)
    assert ci_watch._advanced_past("c" * 40, "d" * 40) is True


def test_advanced_past_reports_false_when_the_pull_request_does_not_contain_it(monkeypatch):
    monkeypatch.setattr(ci_watch, "fetch_commits", lambda root, *shas: None)
    monkeypatch.setattr(ci_watch, "is_ancestor", lambda root, ancestor, descendant: False)
    assert ci_watch._advanced_past("c" * 40, "d" * 40) is False


# REQ: OPS-39
def test_advanced_past_fails_closed_on_an_undeterminable_ancestry(monkeypatch):
    """Issue #3344's own seam contract: an ancestry check that could not be answered must
    read as advanced-past, never as fresh, or a git failure would silently reopen the excuse
    this whole mechanism exists to close."""

    def _raise(*_args, **_kwargs):
        raise TriageError("git fetch failed")

    monkeypatch.setattr(ci_watch, "fetch_commits", _raise)
    assert ci_watch._advanced_past("c" * 40, "d" * 40) is True
