"""The three-way split of FATAL records in ops/fleet/kpi.sh (issue #1670).

One health line asking "did anything write FATAL" cannot be green on a working box.
The fleet's launchers announce a REFUSAL -- a guard that declined and left the system
consistent -- with the same word they use for a fault. Measured on nucbox Thu 10 Sep
2026: every FATAL record in the live 6h window was a launcher refusal, so the line had
no reachable green in normal operation and carried exactly as little information as a
check that can never go red.

Records are now partitioned into `refusal` (counted, never a verdict), `provisioning`
(red, its own line) and `fleet` (red, and the DEFAULT).

Regression lines:
  - [if] a planted stamped fleet FATAL does not turn the fleet line FAIL [then] broken:
    the line must still be reachable-red, or the split has deleted the metric instead
    of sharpening it
  - [if] a planted launcher refusal turns the fleet line FAIL [then] broken: that is the
    defect being fixed
  - [if] a planted refusal does not raise the refusals count [then] broken: it must be
    reclassified, never dropped, or the split has hidden a dispatcher ordering bug
  - [if] a planted provisioning fault does not turn the provisioning line FAIL [then]
    broken, and [if] it turns the FLEET line FAIL [then] also broken: two faults with
    two different owners must not share one line
  - [if] an unrecognised refusal wording lands anywhere but the fleet line [then] broken:
    the classifier is coupled to spawn-worker.sh's exact strings, so the coupling must
    fail RED, never green
  - [if] the untimestamped count stops being reported once the classes exist [then]
    broken: those 27 records are a real backlog of scripts writing FATAL with no stamp
  - [if] the three lines disagree about one planted record [then] broken: they are
    derived from one pass over one input list precisely so they cannot
"""

from __future__ import annotations

import platform
import shutil

import pytest

from tests.scripts.test_ops_fleet_kpi import (
    NOW,
    PROVISIONING_LABEL,
    _copy_fixture,
    _env,
    _fatal_label,
    _health,
    _home,
    _iso,
    _refusals,
    _run,
)

# Duplicated from test_ops_fleet_kpi.py, NOT inherited: pytest marks are module-scoped,
# so importing that module's helpers brings none of its skips, and a pass on macOS or a
# jq-less host would mean nothing for a script documented as GNU-only.
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

# Verbatim shapes from spawn-worker.sh's `fatal` call sites, minus the UTC stamp its
# `fatal` helper prepends. Copied rather than paraphrased: the classifier matches on
# substrings of these, so a test written in its own words would pass while the real
# records fell through.
REFUSAL_OWNED = "FATAL: issue #1515 targets PR #1490, which is OWNED. Two writers on one branch is"
REFUSAL_NO_BRIEF = (
    "FATAL: no brief at /home/dev/jobs/briefs/issue-1515.md "
    "(run mkbrief.sh 1515 first). Refusing to launch a worker with an empty prompt."
)
PROVISIONING_NO_TOKEN = (
    "FATAL: account 'codex-nucbox' has no readable work token in "
    "state/account-tokens (measurable is not spendable)"
)
FLEET_FAULT = "FATAL: redteam trigger failed"


def _plant(fixture, name: str, *records: str) -> None:
    """Write in-window FATAL records into a log the probe reads."""
    (fixture / "jobs" / "logs" / f"{name}.log").write_text(
        "".join(f"{_iso(NOW - 60)} {r}\n" for r in records)
    )


def _lines(tmp_path, *records: str, name: str = "planted") -> tuple[dict[str, str], str]:
    fixture = _copy_fixture(tmp_path)
    if records:
        _plant(fixture, name, *records)
    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    return _health(out), out


def test_the_green_fixture_starts_with_all_three_lines_clean(tmp_path):
    """[if] the baseline is not clean on all three lines [then] fail, [else stop].

    Every case below is a delta from this state, so a baseline that was already red
    would make each of them prove nothing.
    """
    verdicts, out = _lines(tmp_path)
    assert verdicts[_fatal_label(0)] == "PASS", out
    assert verdicts[PROVISIONING_LABEL] == "PASS", out
    assert _refusals(out) == "0", out


def test_a_planted_fleet_fatal_turns_the_fleet_line_red(tmp_path):
    """[if] the fleet line cannot be turned red by a real fault [then] fail, [else stop].

    The control for the whole change. Splitting a permanently-red line into three is
    only an improvement if the resulting fleet line still fails when something is
    genuinely wrong; otherwise the red was replaced with an unfalsifiable green.
    """
    verdicts, out = _lines(tmp_path, FLEET_FAULT)
    assert verdicts[_fatal_label(0)] == "FAIL", out
    # And it stays on its own line: this is not a provisioning problem or a refusal.
    assert verdicts[PROVISIONING_LABEL] == "PASS", out
    assert _refusals(out) == "0", out


@pytest.mark.parametrize(
    ("record", "why"),
    [
        (REFUSAL_OWNED, "a target branch another writer owns"),
        (REFUSAL_NO_BRIEF, "a spawn attempted before its brief was written"),
    ],
)
def test_a_planted_launcher_refusal_leaves_the_fleet_line_green_and_is_counted(
    tmp_path, record, why
):
    """[if] a launcher refusal reddens the fleet line, or goes uncounted [then] fail,
    [else stop].

    Both halves matter and are asserted together. Green alone would be satisfied by a
    classifier that simply discarded the record, which would hide the dispatcher
    ordering bug that issue #1670 was careful not to excuse: the refusal is not a fleet
    fault, but it is still evidence, so it must move from a verdict to a count.
    """
    verdicts, out = _lines(tmp_path, record)
    assert verdicts[_fatal_label(0)] == "PASS", f"{why}: {out}"
    assert verdicts[PROVISIONING_LABEL] == "PASS", f"{why}: {out}"
    assert _refusals(out) == "1", f"{why}: {out}"


def test_a_planted_provisioning_fault_reddens_only_its_own_line(tmp_path):
    """[if] a provisioning fault does not redden its own line, or reddens the fleet
    line too [then] fail, [else stop].

    A lane with no usable token means this box is not set up. That is a real fault and
    stays red, but it is a different person's fault from "the fleet is broken", and a
    line that mixes them tells neither of them anything.
    """
    verdicts, out = _lines(tmp_path, PROVISIONING_NO_TOKEN)
    assert verdicts[PROVISIONING_LABEL] == "FAIL", out
    assert verdicts[_fatal_label(0)] == "PASS", out
    assert _refusals(out) == "0", out


def test_an_unrecognised_refusal_wording_falls_through_to_the_fleet_line(tmp_path):
    """[if] a reworded refusal goes green instead of red [then] fail, [else stop].

    The classifier matches substrings of spawn-worker.sh's message text, so this file
    is coupled to wording in another repo's script and that coupling WILL rot. The
    property that makes the rot survivable is its direction: an unrecognised record
    falls through to `fleet` and turns the board redder, which someone will investigate.
    The opposite default -- treat the unknown as a refusal -- would let a reworded fault
    disappear silently, and no test would ever notice.
    """
    verdicts, out = _lines(
        tmp_path,
        "FATAL: refusing to launch a worker because the target branch has an owner",
    )
    assert verdicts[_fatal_label(0)] == "FAIL", out
    assert _refusals(out) == "0", out


def test_the_untimestamped_backlog_is_still_named_after_the_split(tmp_path):
    """[if] the untimestamped count is dropped once the classes exist [then] fail,
    [else stop].

    27 of these existed on nucbox when the split was written. They can never enter a
    window, so they can never be a verdict, but each one is a script writing FATAL with
    no UTC stamp and that is its own defect (issue #1631). Splitting the verdict lines
    must not quietly retire the count that names them.
    """
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "logs" / "unstamped.log").write_text(
        "FATAL: tick gate failed for merge-odd\n"
        "FATAL: Codex transcript capture failed at exit session_id=missing\n"
    )
    verdicts = _health(_run(_env(fixture, _home(tmp_path, token_profile=True))).stdout)
    # Counted in the label, and still not a verdict: there is no time to window on.
    assert verdicts[_fatal_label(2)] == "PASS"
    assert _fatal_label(0) not in verdicts


def test_one_of_each_class_at_once_lands_on_three_different_lines(tmp_path):
    """[if] the three lines disagree about a mixed batch [then] fail, [else stop].

    The lines are derived from ONE pass over ONE input list precisely so they cannot
    double-count or contradict each other; deriving the same thing twice is the defect
    that cost four P1s on PR #1662 in its other form. This is the test that would catch
    a future edit that re-splits the read.
    """
    verdicts, out = _lines(tmp_path, FLEET_FAULT, REFUSAL_OWNED, PROVISIONING_NO_TOKEN)
    assert verdicts[_fatal_label(0)] == "FAIL", out
    assert verdicts[PROVISIONING_LABEL] == "FAIL", out
    assert _refusals(out) == "1", out


def test_an_out_of_window_record_of_every_class_is_ignored(tmp_path):
    """[if] a record older than the window counts on any of the three lines [then] fail,
    [else stop].

    The window filter used to be the only thing standing between this check and a
    permanent red. Each new line inherits it, so each new line has to prove it.
    """
    fixture = _copy_fixture(tmp_path)
    stale = _iso(NOW - 24 * 3600)
    (fixture / "jobs" / "logs" / "stale.log").write_text(
        f"{stale} {FLEET_FAULT}\n{stale} {REFUSAL_OWNED}\n{stale} {PROVISIONING_NO_TOKEN}\n"
    )
    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    verdicts = _health(out)
    assert verdicts[_fatal_label(0)] == "PASS", out
    assert verdicts[PROVISIONING_LABEL] == "PASS", out
    assert _refusals(out) == "0", out


def test_an_unreadable_log_set_makes_all_three_lines_say_so(tmp_path):
    """[if] any of the three lines yields a number or a verdict it cannot measure [then]
    fail, [else stop].

    Splitting one line into three triples the number of places a green tick can appear
    for a subject nobody read, and the refusals line is the easy one to get wrong: it is
    a count, and a count has a very plausible-looking zero available to it. An unread
    log set must read UNKNOWN there and UNMEASURABLE on both verdict lines.
    """
    fixture = _copy_fixture(tmp_path)
    # A dangling symlink, not a chmod: broken links are unreadable to UID 0 too, so this
    # holds wherever the suite runs (see test_ops_fleet_kpi_unmeasurable.py).
    dangling = fixture / "jobs" / "logs" / "gone.log"
    dangling.symlink_to(fixture / "jobs" / "logs" / "no-such-target.log")
    assert dangling.is_symlink() and not dangling.exists(), "fixture is not actually dangling"

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    verdicts = _health(out)
    assert verdicts[_fatal_label("UNKNOWN")] == "UNMEASURABLE", out
    assert verdicts[PROVISIONING_LABEL] == "UNMEASURABLE", out
    assert _refusals(out) == "UNKNOWN", out
