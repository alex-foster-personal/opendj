"""Health verdicts for a subject kpi.sh cannot read: UNMEASURABLE, not a verdict.

Split out of test_ops_fleet_kpi.py, which sits against the 600-line Python file ceiling
the quality ratchet enforces; trunk has no headroom on that axis, so a module that grows
past it reds the gate for everyone. These cases stand alone anyway: the rest of that file
asks whether kpi.sh reads its inputs CORRECTLY, and these ask what it says when it cannot
read them at all.

[if] a probe that could not measure reports PASS or FAIL [then] fail, [else stop].

Regression lines:
  - [if] an unreadable log tree reports PASS [then] fail, [else stop]. Empty output is what
    a FATAL-free fleet produces too, so the two were indistinguishable.
  - [if] a readable FATAL-free tree stops reporting PASS [then] fail, [else stop]. The
    control: a guard returning UNMEASURABLE for everything satisfies the line above
    perfectly while destroying the only useful state.
  - [if] a partially readable set reports a verdict [then] fail, [else stop]. Returning on
    the first readable file leaves later unreadable ones uninspected, and awk's failure
    does not survive the pipeline.
  - [if] an ordinary probe failure renders as UNMEASURABLE [then] fail, [else stop]. The
    sentinel is an exit code, so it must be one no common tool returns.
"""

from __future__ import annotations

import pytest

from tests.scripts.test_ops_fleet_kpi import (
    _copy_fixture,
    _env,
    _fatal_label,
    _health,
    _home,
    _run,
)

pytestmark = pytest.mark.requirement("OPS-16")

def test_an_unreadable_log_tree_is_unmeasurable_not_a_pass(tmp_path):
    """[if] the FATAL health line reads PASS when its logs cannot be read [then] fail, [else stop].

    The window filter runs awk over a glob with stderr suppressed. Point it at a tree with
    no readable log and the glob stays literal, awk errors into /dev/null, and the output is
    empty, which is also exactly what a genuinely FATAL-free fleet produces. Verified on
    2026-09-09: the line printed PASS with no readable log at all, and the excluded count
    printed 0.

    A failed measurement must report UNMEASURABLE, never a verdict, and least of all the
    reassuring one.
    """
    fixture = _copy_fixture(tmp_path)
    for log in (fixture / "jobs" / "logs").glob("*.log"):
        if log.name != "tick-gate.log":  # gate-log freshness is a different probe
            log.unlink()
    (fixture / "jobs" / "logs" / "tick-gate.log").rename(fixture / "jobs" / "logs" / "tick-gate.keep")
    (fixture / "jobs" / "watchdog.log").unlink(missing_ok=True)

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    verdicts = _health(out)
    label = _fatal_label("UNKNOWN")
    assert label in verdicts, out
    assert verdicts[label] == "UNMEASURABLE", out
    # The whole point: it must not be green, and it must not claim a count it never made.
    assert verdicts[label] != "PASS", out
    assert _fatal_label(0) not in verdicts, out

def test_a_probe_failing_with_a_common_exit_code_is_FAIL_not_unmeasurable(tmp_path):
    """[if] an ordinary probe failure renders as UNMEASURABLE [then] fail, [else stop].

    The UNMEASURABLE sentinel is an exit code, and most probes are a bare command whose
    status passes straight through, so the sentinel must be a value no ordinary tool
    returns. It was 2 for one commit: `grep -qs` returns 2 when its files do not exist, so
    a home directory with no .profile and no .bashrc -- a real FAIL, there is no token --
    reported UNMEASURABLE instead. The canary caught it in 44 seconds on PR #1662.

    This pins the direction that regression went. `token present for launchers` is the
    concrete case, and it is asserted here rather than only inside the red-fixture sweep so
    the reason survives next to the assertion.
    """
    fixture = _copy_fixture(tmp_path)
    home = _home(tmp_path, token_profile=False)  # neither .profile nor .bashrc exists
    verdicts = _health(_run(_env(fixture, home)).stdout)
    assert verdicts["token present for launchers"] == "FAIL"

def test_a_readable_log_tree_with_no_fatal_still_passes(tmp_path):
    """[if] a genuinely FATAL-free fleet stops reporting PASS [then] fail, [else stop].

    The control for the test above. Without it, a guard that returned UNMEASURABLE for
    everything would satisfy that test perfectly and destroy the line's only useful state.
    Same fixture, logs left in place.
    """
    fixture = _copy_fixture(tmp_path)
    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert _health(out)[_fatal_label(0)] == "PASS", out

def test_a_partially_readable_log_set_is_unmeasurable(tmp_path):
    """[if] one unreadable log among readable ones still yields a verdict [then] fail, [else stop].

    Raised as a P1 on PR #1662. The first version of the guard returned success on the
    FIRST readable file, so a set holding one unreadable log passed the check. awk then
    failed on that input into a suppressed stderr, and with no `pipefail` the downstream
    non-match made the probe report success, so the line could read PASS while some logs
    were never inspected. Partial coverage is not coverage.
    """
    fixture = _copy_fixture(tmp_path)
    logs = fixture / "jobs" / "logs"
    blinded = logs / "resident-blinded.log"
    blinded.write_text("2026-01-01T00:00:00Z nothing to see\n")
    blinded.chmod(0o000)
    try:
        verdicts = _health(_run(_env(fixture, _home(tmp_path, token_profile=True))).stdout)
        assert verdicts[_fatal_label("UNKNOWN")] == "UNMEASURABLE"
    finally:
        blinded.chmod(0o644)
