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
"""

from __future__ import annotations

import inspect

import pytest

from scripts import ci_watch
from scripts.ci_failure_ids import JobVerdict
from scripts.ci_main_red import LogUnreadable, MainRed
from scripts.ci_wait_core import WaitStatus, poll_until_terminal
from scripts.ci_watch import Exit, FailureWatch, exit_for

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
    job unmeasured, and the watcher could never report a pull request as clean again."""
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
