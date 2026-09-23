"""nucbox probe verdicts (AGENT-16).

Every test here exists because the opposite behavior would be invisible: a
health aggregate that reads "ok" for a box nobody could reach looks exactly
like a healthy fleet.
"""

from __future__ import annotations

import pytest

from apps.fleet_mcp import nucbox
from apps.fleet_mcp.runner import UNKNOWN, Completed
from tests.fleet_mcp.conftest import fake_runner, unknown_runner


@pytest.mark.requirement("AGENT-16")
def test_pressure_ok_line_parses():
    """[if] pressure.sh prints PRESSURE ok [then] the probe reports ok, [else stop]"""
    document = nucbox.probe("pressure", runner=fake_runner(stdout="PRESSURE ok top=ci:12%/3p"))
    assert document["status"] == "ok"


@pytest.mark.requirement("AGENT-16")
def test_pressure_high_line_parses():
    """[if] pressure.sh prints PRESSURE high [then] the probe reports high, [else stop]"""
    document = nucbox.probe(
        "pressure", runner=fake_runner(stdout="PRESSURE high reason=mem_avail<6GB")
    )
    assert document["status"] == "high"
    assert "reason=mem_avail" in document["line"]


@pytest.mark.requirement("AGENT-16")
def test_unrecognized_pressure_line_is_unknown_not_ok():
    """[if] the gate line cannot be parsed [then] the probe reports UNKNOWN, [else stop]

    An unparsed line is a failed measurement. Reporting it as ok would let a
    broken or renamed pressure.sh read as a green spawn gate.
    """
    document = nucbox.probe("pressure", runner=fake_runner(stdout="usage: pressure.sh [-v]"))
    assert document["status"] == UNKNOWN
    assert "unrecognized" in document["reason"]


@pytest.mark.requirement("AGENT-16")
def test_ssh_transport_failure_is_unknown():
    """[if] ssh itself fails (exit 255) [then] the probe reports UNKNOWN, not error, [else stop]"""
    document = nucbox.probe(
        "kpi", runner=fake_runner(returncode=255, stderr="ssh: connect to host port 22: timeout")
    )
    assert document["status"] == UNKNOWN
    assert nucbox.NUCBOX_SSH_HOST in document["reason"]


@pytest.mark.requirement("AGENT-16")
def test_remote_nonzero_exit_is_a_measurement():
    """Negative control: a remote command's own failure is measured, not UNKNOWN.

    [if] the remote command exits nonzero for its own reasons [then] the box WAS, [else stop]
    reachable, and collapsing that into UNKNOWN would hide a real red signal.
    """
    document = nucbox.probe("kpi", runner=fake_runner(returncode=2, stderr="kpi.sh: no data"))
    assert document["status"] == "error"
    assert document["returncode"] == 2


@pytest.mark.requirement("AGENT-16")
def test_no_tmux_server_is_zero_workers_not_unknown():
    """[if] tmux reports no server running [then] that is a measured zero workers, [else stop]"""
    document = nucbox.probe(
        "workers", runner=fake_runner(returncode=1, stderr="no server running on /tmp/tmux-1000")
    )
    assert document["status"] == "measured"
    assert document["live_workers"] == 0


@pytest.mark.requirement("AGENT-16")
def test_live_worker_sessions_are_counted():
    """[if] tmux lists job-<N> sessions [then] they are counted as live workers, [else stop]"""
    listing = "job-1801: 1 windows\nrc-qa: 1 windows\njob-1802: 1 windows\n"
    document = nucbox.probe("workers", runner=fake_runner(stdout=listing))
    assert document["live_workers"] == 2
    assert document["sessions"] == ["job-1801", "job-1802"]


@pytest.mark.requirement("AGENT-16")
def test_health_refuses_a_verdict_when_the_box_is_unreachable():
    """[if] no probe could be measured [then] health is UNKNOWN and names them, [else stop]"""
    document = nucbox.health(runner=unknown_runner("ssh nucbox-wsl unreachable"))
    assert document["status"] == UNKNOWN
    assert set(document["unmeasured"]) == set(nucbox.HEALTH_PROBES)
    assert "no health verdict is claimed" in document["reason"]


@pytest.mark.requirement("AGENT-16")
def test_health_reports_ok_only_when_every_probe_measured():
    """[if] every probe is measured and unpressured [then] health reads ok, [else stop]

    Positive control for the UNKNOWN case above: the probe must be able to
    return ok, or its refusals prove nothing.
    """
    document = nucbox.health(runner=fake_runner(stdout="PRESSURE ok"))
    assert document["status"] == "ok"
    assert "unmeasured" not in document


@pytest.mark.requirement("AGENT-16")
def test_a_dead_watchdog_is_not_an_ok_spawn_gate():
    """[if] a gate probe RAN and failed [then] health refuses ok, [else stop]

    The fail-open this exists for: `systemctl --user is-active` exits nonzero
    for an INACTIVE watchdog, which the ssh layer reports as `error`, not
    UNKNOWN. Keying the aggregate on UNKNOWN alone published `ok` for a dead
    watchdog and would have sent callers to spawn into an unhealthy fleet
    (Codex P1, #3735). Absence of UNKNOWN is not presence of health.
    """

    def runner(argv, *, timeout):
        remote = argv[-1]
        if "is-active" in remote:
            return Completed(tuple(argv), 3, "inactive", "")
        if remote == "tmux ls":
            return Completed(tuple(argv), 0, "job-1801: 1 windows", "")
        return Completed(tuple(argv), 0, "PRESSURE ok", "")

    document = nucbox.health(runner=runner)
    assert document["status"] == "error"
    assert document["failed"] == ["watchdog"]
    assert "the spawn gate is not ok" in document["reason"]
    # The pressure probe really did read ok; the refusal comes from the
    # watchdog, not from a probe that failed to measure.
    assert document["probes"]["pressure"]["status"] == "ok"
    assert "unmeasured" not in document


@pytest.mark.requirement("AGENT-16")
def test_a_failed_worker_probe_is_not_an_ok_spawn_gate():
    """[if] the worker probe errors for an unrecognized reason [then] not ok, [else stop]

    Same class as the watchdog case, other probe: `tmux ls` failing for
    anything but the known no-server line is a failed measurement of the
    workers, and must not ride pressure's ok to an overall pass.
    """

    def runner(argv, *, timeout):
        remote = argv[-1]
        if remote == "tmux ls":
            return Completed(tuple(argv), 1, "", "error connecting to /tmp/tmux-1000/default")
        if "is-active" in remote:
            return Completed(tuple(argv), 0, "active", "")
        return Completed(tuple(argv), 0, "PRESSURE ok", "")

    document = nucbox.health(runner=runner)
    assert document["status"] == "error"
    assert document["failed"] == ["workers"]


@pytest.mark.requirement("AGENT-16")
def test_no_server_running_is_still_a_measured_zero_and_stays_ok():
    """[if] tmux reports no server [then] that is zero workers, not a failure, [else stop]

    The control that keeps the rule above from over-correcting: an empty tmux
    is the normal idle fleet, and must still reach ok.
    """

    def runner(argv, *, timeout):
        remote = argv[-1]
        if remote == "tmux ls":
            return Completed(tuple(argv), 1, "", "no server running on /tmp/tmux-1000/default")
        if "is-active" in remote:
            return Completed(tuple(argv), 0, "active", "")
        return Completed(tuple(argv), 0, "PRESSURE ok", "")

    document = nucbox.health(runner=runner)
    assert document["status"] == "ok"
    assert document["probes"]["workers"]["live_workers"] == 0


@pytest.mark.requirement("AGENT-16")
def test_unknown_probe_name_is_refused_before_any_subprocess():
    """[if] a caller names a probe off the allowlist [then] nothing is executed, [else stop]"""

    def explode(argv, *, timeout):
        raise AssertionError("no subprocess may run for an unknown probe")

    with pytest.raises(ValueError, match="unknown probe"):
        nucbox.probe("rm -rf ~/jobs", runner=explode)


@pytest.mark.requirement("AGENT-16")
def test_log_line_count_is_clamped_to_an_integer():
    """[if] a caller asks for an absurd tail [then] the count is clamped, [else stop]"""
    runner = fake_runner(stdout="tick")
    document = nucbox.dispatcher_log(10**9, runner=runner)
    assert document["lines_requested"] == nucbox.MAX_LOG_LINES
