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

import os
import platform
import shutil
import stat

import pytest

from tests.platform_capabilities import posix_permission_denial_supported
from tests.scripts.test_ops_fleet_kpi import (
    NOW,
    _copy_fixture,
    _env,
    _fatal_label,
    _health,
    _home,
    _iso,
    _run,
)

#: chmod cannot create an unreadable file for UID 0, which is how CI runs here: root
#: still satisfies -r, so the test would assert against a fixture whose stated
#: precondition never held. The dangling-symlink case below needs no such gate,
#: because a broken link is unreadable to root as well.
_CAN_DENY_PERMISSION = posix_permission_denial_supported(os.name, getattr(os, "geteuid", None))

# Duplicated from test_ops_fleet_kpi.py, NOT inherited: pytest marks are module-scoped, so
# importing that module's helpers brings none of its skips. Without these, the five cases
# below would run kpi.sh on macOS or a jq-less host -- a script documented as GNU-only --
# and a pass there would mean nothing. Raised as a P2 on PR #1662.
pytestmark = [
    pytest.mark.requirement("OPS-16"),
    pytest.mark.skipif(
        platform.system() != "Linux",
        reason="ops/fleet/kpi.sh is Linux-only: GNU date -d/stat -c, /proc, systemd --user, tmux",
    ),
    pytest.mark.skipif(
        shutil.which("jq") is None,
        reason="ops/fleet/kpi.sh parses the gh JSON fixtures with jq",
    ),
]

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

@pytest.mark.skipif(not _CAN_DENY_PERMISSION, reason="chmod cannot deny root; see the dangling-symlink case")
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
    # NOT resident-*.log. That name makes kpi.sh's retired-lane scan read the file too,
    # and an unreadable one there aborts the whole script with exit 2 before the health
    # section runs, so this test was asserting against a board that was never printed.
    # The failure was real and loud, but it was not the failure this test names.
    blinded = logs / "blinded.log"
    blinded.write_text("2026-01-01T00:00:00Z nothing to see\n")
    blinded.chmod(0o000)
    try:
        verdicts = _health(_run(_env(fixture, _home(tmp_path, token_profile=True))).stdout)
        assert verdicts[_fatal_label("UNKNOWN")] == "UNMEASURABLE"
    finally:
        blinded.chmod(0o644)


def test_a_dangling_symlink_in_the_log_glob_is_unmeasurable(tmp_path):
    """[if] a dangling symlink among the logs still yields a verdict [then] fail, [else stop].

    Raised as a second P1 on PR #1662, after the first readability fix. The glob matches a
    symlink by NAME, so awk is handed the path, but `-e` follows the link and reports false
    because the target is gone. The guard therefore skipped it as though the glob had not
    matched, while awk failed on it into the suppressed stderr and the line still read PASS.

    This case also carries the root problem the chmod test cannot: a broken link is
    unreadable to UID 0 too, so this assertion holds wherever the suite runs.
    """
    fixture = _copy_fixture(tmp_path)
    dangling = fixture / "jobs" / "logs" / "gone.log"
    dangling.symlink_to(fixture / "jobs" / "logs" / "no-such-target.log")
    assert dangling.is_symlink() and not dangling.exists(), "fixture is not actually dangling"

    verdicts = _health(_run(_env(fixture, _home(tmp_path, token_profile=True))).stdout)
    assert verdicts[_fatal_label("UNKNOWN")] == "UNMEASURABLE"


@pytest.mark.skipif(not _CAN_DENY_PERMISSION, reason="chmod cannot deny root")
def test_an_unenumerable_logs_directory_is_unmeasurable(tmp_path):
    """[if] a logs directory that cannot be listed reports a count [then] fail, [else stop].

    Third P1 on PR #1662, and the one the previous two fixes walked straight past. `-d`
    succeeds on a directory that cannot be enumerated, `nullglob` then yields zero entries,
    and a readable watchdog.log was enough to mark the whole set measurable. Every file
    under logs/ went silently unread while the line printed +0 and PASS.

    Zero entries from a directory you cannot open is not an empty directory. It is the same
    defect as the empty-awk-output one this module exists for, one level up the tree.
    """
    fixture = _copy_fixture(tmp_path)
    logs = fixture / "jobs" / "logs"
    (fixture / "jobs" / "watchdog.log").write_text("2026-01-01T00:00:00Z fine\n")
    logs.chmod(0o000)
    try:
        verdicts = _health(_run(_env(fixture, _home(tmp_path, token_profile=True))).stdout)
        assert verdicts[_fatal_label("UNKNOWN")] == "UNMEASURABLE"
    finally:
        logs.chmod(0o755)


def test_a_real_fatal_in_watchdog_log_is_seen_when_no_other_log_exists(tmp_path):
    """[if] an in-window FATAL in watchdog.log is missed when logs/ holds no *.log [then] fail, [else stop].

    Fourth P1 on PR #1662, and the one that named the real defect: the guard validated an
    input set while `_fatal_lines` re-derived it with its own glob. With no `*.log` present
    the guard passed on a readable watchdog.log and then restored nullglob, so `_fatal_lines`
    handed awk the literal unmatched `logs/*.log`, awk stopped on that read error, and a
    genuine FATAL went unreported as PASS.

    This is the only test here that asserts FAIL rather than UNMEASURABLE, and it is the
    most important one: the whole module guards against a green tick that means nothing, and
    a probe silently missing a REAL fault is that failure in its worst form.
    """
    fixture = _copy_fixture(tmp_path)
    for log in (fixture / "jobs" / "logs").glob("*.log"):
        if log.name != "tick-gate.log":
            log.unlink()
    (fixture / "jobs" / "logs" / "tick-gate.log").rename(
        fixture / "jobs" / "logs" / "tick-gate.keep"
    )
    stamp = _iso(NOW - 60)
    (fixture / "jobs" / "watchdog.log").write_text(f"{stamp} FATAL: a real one\n")

    verdicts = _health(_run(_env(fixture, _home(tmp_path, token_profile=True))).stdout)
    assert verdicts[_fatal_label(0)] == "FAIL"


def test_a_clean_watchdog_log_alone_still_passes(tmp_path):
    """[if] a clean watchdog.log with no other log stops passing [then] fail, [else stop].

    The control for the case above. A guard that reported FAIL for every watchdog-only
    fixture would satisfy that test perfectly while making the line useless.
    """
    fixture = _copy_fixture(tmp_path)
    for log in (fixture / "jobs" / "logs").glob("*.log"):
        if log.name != "tick-gate.log":
            log.unlink()
    (fixture / "jobs" / "logs" / "tick-gate.log").rename(
        fixture / "jobs" / "logs" / "tick-gate.keep"
    )
    (fixture / "jobs" / "watchdog.log").write_text("nothing of interest here\n")

    verdicts = _health(_run(_env(fixture, _home(tmp_path, token_profile=True))).stdout)
    assert verdicts[_fatal_label(0)] == "PASS"


def test_a_directory_named_like_a_log_is_unmeasurable(tmp_path):
    """[if] a directory matching logs/*.log yields a verdict [then] fail, [else stop].

    Fifth P1 on PR #1662. `-r` succeeds on a directory, so it entered the input list; gawk
    warns that it skipped it and carries on, leaving +0 and PASS. A skipped input is an
    unread input, and the warning went to a stderr nobody reads.
    """
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "logs" / "adirectory.log").mkdir()
    verdicts = _health(_run(_env(fixture, _home(tmp_path, token_profile=True))).stdout)
    assert verdicts[_fatal_label("UNKNOWN")] == "UNMEASURABLE"


def test_a_fifo_named_like_a_log_does_not_hang_the_board(tmp_path):
    """[if] a FIFO matching logs/*.log is opened by the probe [then] fail, [else stop].

    The other half of the same P1, and the more dangerous half. A readable FIFO passes `-r`,
    and awk opening it blocks until a writer appears, which for an unattended KPI run means
    forever: no health line, no failure, no board at all.

    The assertion is deliberately reached through a real run rather than a mocked one. If
    the guard regresses, this test does not fail, it HANGS, and pytest's own timeout is what
    reports it. That is the honest shape here, because the defect IS a hang.
    """
    fixture = _copy_fixture(tmp_path)
    fifo = fixture / "jobs" / "logs" / "afifo.log"
    os.mkfifo(fifo)
    try:
        assert stat.S_ISFIFO(fifo.stat().st_mode), "fixture is not actually a FIFO"
        verdicts = _health(_run(_env(fixture, _home(tmp_path, token_profile=True))).stdout)
        assert verdicts[_fatal_label("UNKNOWN")] == "UNMEASURABLE"
    finally:
        fifo.unlink()


def test_a_logs_path_that_is_not_a_directory_is_unmeasurable(tmp_path):
    """[if] a non-directory logs path yields a verdict [then] fail, [else stop].

    Sixth P1 on PR #1662. The guard only looked INSIDE logs/ when `-d` said it was a
    directory. A logs path that exists as a regular file made `-d` false, the whole branch
    was skipped, and a readable watchdog.log alone made the input set non-empty. The line
    then reported PASS having omitted the entire logs source.

    Same principle as every other case here, applied to one more path: a name that is ABSENT
    contributes nothing and that is fine, a name that EXISTS but is not usable is
    unmeasurable. Six findings on this PR were six paths where that was not yet true.
    """
    fixture = _copy_fixture(tmp_path)
    shutil.rmtree(fixture / "jobs" / "logs")
    (fixture / "jobs" / "logs").write_text("not a directory\n")
    (fixture / "jobs" / "watchdog.log").write_text("readable and quiet\n")

    verdicts = _health(_run(_env(fixture, _home(tmp_path, token_profile=True))).stdout)
    assert verdicts[_fatal_label("UNKNOWN")] == "UNMEASURABLE"


def test_an_absent_logs_directory_with_a_clean_watchdog_still_passes(tmp_path):
    """[if] an absent logs directory stops the line passing [then] fail, [else stop].

    The control for the case above, and the one that keeps the rule honest. ABSENT and
    MALFORMED must not collapse into the same answer: a guard that refused whenever logs/
    was not a readable directory would satisfy that test perfectly, and would also report
    UNMEASURABLE on a fleet that simply has not written a log yet.
    """
    fixture = _copy_fixture(tmp_path)
    shutil.rmtree(fixture / "jobs" / "logs")
    (fixture / "jobs" / "watchdog.log").write_text("readable and quiet\n")

    verdicts = _health(_run(_env(fixture, _home(tmp_path, token_profile=True))).stdout)
    assert verdicts[_fatal_label(0)] == "PASS"
