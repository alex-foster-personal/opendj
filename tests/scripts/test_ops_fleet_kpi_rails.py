"""Wiring tests for the fleet-parallelism rails in ops/fleet/kpi.sh.

Split out of test_ops_fleet_kpi.py so neither module carries a second concern:
that one covers ticks, throughput, backlog, SLA and health; this one covers the
four rails the maintainer asked for on Sun 6 Sep 2026 - the per-account five-hour gate,
account flips, the self-merge rate and duplicate-fix incidents.

Same hermetic harness, imported rather than copied: the KPI_* seams point every
external read at the committed fixtures under tests/fixtures/fleet-kpi/, and the
window is frozen against KPI_NOW_UNIX so assertions never rot.

The rails exist to make a FAILED measurement visible, so every test here has a
partner that removes the input and asserts the line says so. A rail that renders
"unmeasurable" as 0 is worse than no rail at all.

Regression lines:
  - if any five-hour gate state is misreported then broken (allow / deny at 85 /
    flip at 90 / deny on a stale reading / deny on a missing reading /
    unmeasurable when no credential can read that account, one fixture lane each)
  - if staleness is decided by the stamp file's MTIME rather than its CONTENT
    then broken (the measurer re-emits a cached reading under its original read
    time, so an mtime makes a rate-limited meter look permanently fresh)
  - if a missing lane->account map renders as "no lanes" rather than a broken
    rail then broken
  - if an account flip from a previous UTC day counts toward today then broken
    (3 logged flips, 2 of them today)
  - if a missing flip log is indistinguishable from a quiet day then broken
  - if an out-of-window merge stamp counts toward the self-merge rate then
    broken (4 stamps, 3 in window -> 2 self + 1 merge-lane = 67%)
  - if an unstamped window renders as a 0% self-merge rate then broken
  - if a partly-stamped window (fewer stamps than the window's real merge
    count) prints a clean percent instead of naming both numbers as
    partly-measured then broken (OPS-24: 1 stamp of 10 merges must not read
    as a measured 100%, and 1 merge-lane stamp of 10 merges must not read as
    a measured 0%)
  - if the merged-PR count itself is unmeasurable and the self-merge rate
    still prints a clean percent then broken (a rate cannot be checked for
    completeness against a denominator that was never read)
  - if a MERGED PR carrying duplicate/superseded markers counts as a
    duplicate-fix incident then broken (only closed-unmerged ones count)
  - if an unreadable closed-PR fixture reports 0 duplicates then broken
  - if gate=flip is printed for an account with no next account written in
    state/account-rotation then broken (a spawn would proceed onto a walled
    account with nothing behind it)
  - if state/lane-accounts exists but declares no usable row (all comments and
    blanks) and the rail simply goes silent instead of naming a broken map
    then broken
  - if a lane-accounts row with no trailing newline is silently dropped by the
    read loop then broken
  - if the closed-PR read hits its --limit cap and the truncated count is
    printed as a measured value instead of unmeasurable then broken
  - if an account with no seat anywhere in state/account-seats reports
    gate=unmeasurable (the most permissive outcome) instead of gate=deny (the
    same refusal every other unread-meter case already gets) then broken
  - if gate=flip fires for a rotation successor that itself has no seat, no
    fresh reading, or is at/above the deny threshold then broken (a rotation
    row only proves someone INTENDED a flip, never that the target can spawn)
  - if a negative reading age (clock skew) is treated as permanently fresh
    instead of UNKNOWN then broken
  - if a merge-log line with an unrecognized kind= silently shrinks the
    self-merge denominator instead of making the rate unmeasurable then broken
  - if an absent account-flip log renders as today=0 instead of naming the
    absent file as unmeasurable then broken
  - if an unparseable five-hour reading or a corrupt closed-PR JSON payload
    aborts the script instead of reaching its own named branch then broken
    (this script only sets `set -u`, never `set -e`/`pipefail`, so a bad
    command substitution empties a variable rather than exiting)
"""

from __future__ import annotations

import json
import os

from tests.scripts.test_ops_fleet_kpi import (
    NOW,
    _copy_fixture,
    _env,
    _home,
    _run,
    pytestmark,
)

__all__ = ["pytestmark"]

# visible. Every test below therefore has a partner that removes the input and
# asserts the line says so, because a rail that renders "unmeasurable" as 0 is
# worse than no rail at all.


def test_five_hour_rail_reports_one_line_per_lane_with_its_gate_state(tmp_path):
    """If any five-hour gate state is misreported then broken.

    Six fixture lanes, one per branch: allow, deny at 85, flip at 90, deny on a
    stale reading, deny on a missing reading, and deny for an account no
    credential on this host can read (the most severe unknown must resolve to
    the same refusal as the others, never to the most permissive outcome).
    """
    proc = _run(_env(_copy_fixture(tmp_path), _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out

    assert "account lane=frontend-hotspots account=acct-green five_hour_pct=42 age_s=300 gate=allow" in out
    assert "account lane=backend account=acct-warm five_hour_pct=87 age_s=300 gate=deny" in out
    assert "account lane=ci-infra account=acct-hot five_hour_pct=91 age_s=300 gate=flip" in out
    # 2500s old against a 2400s bound: the reading exists and is still refused.
    assert "account lane=docs-reqs account=acct-stale five_hour_pct=UNKNOWN age_s=2500 gate=deny" in out
    assert "account lane=probe account=acct-noread five_hour_pct=UNKNOWN age_s=none gate=deny" in out
    assert "account lane=workers account=acct-nomeasure five_hour_pct=UNKNOWN age_s=none gate=deny" in out
    assert "gate=unmeasurable" not in out

    # The stale and unreadable lanes must never render as a number, in either
    # direction: not 0 (which reads as a healthy account) and not 100.
    for lane in ("docs-reqs", "probe", "workers"):
        line = next(ln for ln in out.splitlines() if ln.startswith(f"account lane={lane} "))
        assert "five_hour_pct=UNKNOWN" in line, line


def test_a_stale_reading_is_decided_by_the_stamp_content_not_the_file_mtime(tmp_path):
    """If staleness is read from the mtime then broken.

    The measurer re-emits a cached endpoint answer under its ORIGINAL read time,
    so touching the stamp file must not make a 41-minute-old reading fresh. This
    is the one case that can tell the two implementations apart.
    """
    fixture = _copy_fixture(tmp_path)
    stamp = fixture / "jobs" / "state" / "acct-stale-five-hour-measured-at"
    os.utime(stamp, (NOW, NOW))

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert "account lane=docs-reqs account=acct-stale five_hour_pct=UNKNOWN age_s=2500 gate=deny" in out


def test_lane_accounts_absent_is_a_broken_rail_not_an_empty_one(tmp_path):
    """If a missing lane->account map renders as no lanes then broken."""
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "state" / "lane-accounts").unlink()

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert "account lane=none account=none five_hour_pct=UNKNOWN age_s=none gate=unmeasurable" in out
    assert "state/lane-accounts absent" in out


def test_account_flips_count_the_utc_day_only(tmp_path):
    """If a flip from a previous UTC day counts then broken (3 lines, 2 today).

    Also the control for the absent-log test below: a present, readable log
    with real flip lines must report the real count, never unmeasurable.
    """
    out = _run(_env(_copy_fixture(tmp_path), _home(tmp_path, token_profile=True))).stdout
    assert "account-flips today=2 lanes=backend,ci-infra" in out
    assert "today=unmeasurable" not in out


def test_absent_flip_log_names_the_file_instead_of_reporting_a_bare_zero(tmp_path):
    """If a missing flip log is indistinguishable from a quiet day then broken.

    A missing log and a genuinely quiet day must never render as the same
    number: 0 is a value AND kpi.sh's own error signature (OPS-24), so the
    absent-file case is unmeasurable, naming the file, never a bare 0.
    """
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "logs" / "account-flip.log").unlink()

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert "account-flips today=unmeasurable lanes=none" in out
    assert "ABSENT; no flip has ever been logged" in out
    assert "today=0" not in out


def test_self_merge_rate_is_taken_from_stamped_merges_in_the_window(tmp_path):
    """If an out-of-window merge stamp counts then broken.

    Four stamps, three in the 1h window: 2 self and 1 merge-lane -> 67%. The
    denominator names the stamped count against the merged count, so an
    unstamped merge deflates neither bucket silently.
    """
    out = _run(_env(_copy_fixture(tmp_path), _home(tmp_path, token_profile=True))).stdout
    assert "self-merge rate=67% own_lane=2 merge_lane=1" in out
    assert "denominator = the 3 STAMPED merges of 3 merged" in out


def test_no_merge_stamps_is_unmeasurable_not_a_zero_percent_self_merge_rate(tmp_path):
    """If an unstamped window renders as 0% self-merge then broken."""
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "logs" / "merge-events.log").unlink()

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert "self-merge rate=unmeasurable own_lane=0 merge_lane=0" in out
    assert "rate=0%" not in out


def _merged(numbers: list[int]) -> str:
    return json.dumps([{"number": n} for n in numbers])


def test_a_partly_stamped_window_is_unmeasurable_not_a_clean_hundred_percent(tmp_path):
    """If 1 stamp of 10 merges in the window renders as a measured 100% then
    broken (OPS-24, #1605 evidence). The fixture's own_lane stamp (pr=1131,
    kind=self, in-window) is the only stamped merge; the other 9 numbers on
    the merged-PR line never got a lane stamp at all, so the rate must name
    both counts and refuse a clean percent."""
    fixture = _copy_fixture(tmp_path)
    log = fixture / "jobs" / "logs" / "merge-events.log"
    log.write_text("2026-09-04T17:45:00Z MERGE lane=backend pr=1131 kind=self\n")
    (fixture / "gh" / "merged.json").write_text(_merged([1131, *range(2000, 2009)]))

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert "merges n=10 " in out
    assert (
        "self-merge rate=unmeasurable own_lane=1 merge_lane=0 (1 of 10 merges "
        "in the 1h window are stamped; partly-measured, not a clean rate)" in out
    )
    assert "rate=100%" not in out


def test_a_partly_stamped_window_never_reads_as_a_clean_zero_percent_either(tmp_path):
    """The overshoot control for the test above: own_lane=0 over an equally
    partial denominator must not print as a measured 0% self-merge rate
    either (OPS-24 acceptance: 'never as a 0% self-merge rate'). Only the
    lane stamp is present; the other 9 merges have no stamp at all."""
    fixture = _copy_fixture(tmp_path)
    log = fixture / "jobs" / "logs" / "merge-events.log"
    log.write_text("2026-09-04T18:10:00Z MERGE lane=merge-odd pr=1133 kind=merge-lane\n")
    (fixture / "gh" / "merged.json").write_text(_merged([1133, *range(2000, 2009)]))

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert "merges n=10 " in out
    assert (
        "self-merge rate=unmeasurable own_lane=0 merge_lane=1 (1 of 10 merges "
        "in the 1h window are stamped; partly-measured, not a clean rate)" in out
    )
    assert "rate=0%" not in out


def test_self_merge_rate_is_unmeasurable_when_the_merged_pr_count_itself_is_unmeasurable(
    tmp_path,
):
    """If the GitHub merged-PR read fails and the self-merge rate still prints a
    clean percent then broken: the rate cannot be checked for completeness
    against a denominator the script never read (OPS-24)."""
    fixture = _copy_fixture(tmp_path)
    (fixture / "gh" / "merged.json").unlink()

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr
    assert "merges n=unmeasurable" in out
    assert (
        "self-merge rate=unmeasurable own_lane=2 merge_lane=1 (3 merge(s) "
        "stamped in the 1h window, but the window's merged-PR count is "
        "unmeasurable" in out
    )
    assert "rate=67%" not in out


def test_an_unclassified_kind_shrinks_the_denominator_not_silently(tmp_path):
    """If a merge-log line with neither kind=self nor kind=merge-lane silently
    drops out of the denominator then broken - a partly-broken log-merge.sh
    would otherwise read as a fully-measured rate over fewer stamps than were
    actually written. One extra in-window line, kind=other, added to the three
    already-classified in-window stamps (2 self + 1 merge-lane)."""
    fixture = _copy_fixture(tmp_path)
    log = fixture / "jobs" / "logs" / "merge-events.log"
    with log.open("a") as f:
        f.write("2026-09-04T18:05:00Z MERGE lane=rc-qa pr=1140 kind=other\n")

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert (
        "self-merge rate=unmeasurable own_lane=2 merge_lane=1 (1 in-window "
        "merge-log line(s) carry neither kind=self nor kind=merge-lane; "
        "refusing a rate over an undercounted denominator)" in out
    )
    assert "rate=67%" not in out


def test_duplicate_fix_counts_closed_unmerged_superseded_prs_only(tmp_path):
    """If a MERGED PR carrying the same markers counts as a duplicate then broken.

    The fixture holds four closed PRs: one closed-unmerged with a 'Superseded by
    #1304' body, one closed-unmerged labeled duplicate, one MERGED PR carrying
    both markers, and one closed-unmerged with neither. Only the first two are
    incidents.
    """
    out = _run(_env(_copy_fixture(tmp_path), _home(tmp_path, token_profile=True))).stdout
    assert "duplicate-fix superseded_prs_today=2 prs=[1306,1327]" in out


def test_a_failed_closed_pr_read_refuses_a_silent_zero(tmp_path):
    """If an unreadable closed-PR fixture reports 0 duplicates then broken."""
    fixture = _copy_fixture(tmp_path)
    (fixture / "gh" / "closed.json").unlink()

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr
    assert "duplicate-fix superseded_prs_today=unmeasurable prs=[]" in out
    assert "superseded_prs_today=0" not in out
    assert "refusing a silent empty read" in proc.stderr


def test_flip_gate_refuses_an_account_with_no_next_account_written(tmp_path):
    """If gate=flip is printed with no rotation entry then broken.

    acct-hot is the fixture's only flip lane (91%) and state/account-rotation
    maps it to acct-cold, which is what keeps the happy-path flip test above
    green. Delete that one row here and the SAME account must fall to
    gate=deny naming the missing rotation entry, never render the walled
    account as an action.
    """
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "state" / "account-rotation").write_text(
        "# acct-hot deliberately has no row in this fixture variant\n"
    )

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert (
        "account lane=ci-infra account=acct-hot five_hour_pct=91 age_s=300 "
        "gate=deny (>=90 but no next account written in state/account-rotation "
        "for acct-hot; refusing rather than flipping onto nothing)" in out
    )
    assert "gate=flip" not in out


def test_flip_gate_refuses_a_successor_with_no_seat_or_reading(tmp_path):
    """If gate=flip fires for an unverifiable successor then broken.

    A row in state/account-rotation only proves someone INTENDED a rotation,
    never that the target account can currently take the spawn. Point
    acct-hot's rotation entry at acct-nomeasure - the same account the
    five-hour rail itself already refuses to measure, no seat anywhere in
    state/account-seats - and the flip must not render that account as a
    healthy landing pad.
    """
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "state" / "account-rotation").write_text(
            "ci-infra acct-hot acct-nomeasure\n"
    )

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert (
        "account lane=ci-infra account=acct-hot five_hour_pct=91 age_s=300 "
        "gate=deny (>=90: successor acct-nomeasure in state/account-rotation "
        "has no seat, no fresh reading, or is itself at/above 85%; refusing "
        "rather than flipping onto an unverified account)" in out
    )
    assert "gate=flip" not in out


def test_flip_gate_refuses_a_successor_that_is_itself_walled(tmp_path):
    """If gate=flip fires for a successor at or above the deny threshold then
    broken - a rotation entry pointing at another walled account is exactly
    the case OPS-23 exists to refuse."""
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "state" / "account-rotation").write_text(
            "ci-infra acct-hot acct-warm\n"  # acct-warm reads 87%, itself >= FIVE_HOUR_DENY_PCT
    )

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert (
        "account lane=ci-infra account=acct-hot five_hour_pct=91 age_s=300 "
        "gate=deny (>=90: successor acct-warm in state/account-rotation" in out
    )
    assert "gate=flip" not in out


def test_a_negative_reading_age_is_never_treated_as_permanently_fresh(tmp_path):
    """If a negative age (clock skew) reads as fresh instead of UNKNOWN then
    broken. Staleness only checked `-gt FIVE_HOUR_MAX_AGE_S`, so a stamp
    written in the future - clock skew on the measuring host, or a wrong date -
    never tripped the bound and served the percent as live indefinitely."""
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "state" / "acct-green-five-hour-measured-at").write_text(
        "2026-09-04T19:00:00Z\n"  # 30 minutes AHEAD of NOW (18:30:00Z)
    )

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert (
        "account lane=frontend-hotspots account=acct-green five_hour_pct=UNKNOWN "
        "age_s=-1800 gate=deny (negative age: clock skew" in out
    )
    assert "gate=allow" not in out


def _write_seven_day(fixture, account: str, pct: str, stamp: str) -> None:
    state = fixture / "jobs" / "state"
    (state / f"{account}-seven-day-pct").write_text(f"{pct}\n")
    (state / f"{account}-seven-day-measured-at").write_text(f"{stamp}\n")


def test_a_spent_weekly_window_denies_a_lane_the_five_hour_meter_would_allow(tmp_path):
    """If a lane reads gate=allow while its account's fresh seven-day reading is
    at or above the weekly mark then broken. Fri 11 Sep 2026: account3 read
    five_hour_pct=0 and seven_day_pct=100, can-spawn.sh's weekly rail refused
    every one of its lanes, both merge lanes ticked into "hit your weekly
    limit", and this board printed gate=allow for all of them."""
    fixture = _copy_fixture(tmp_path)
    _write_seven_day(fixture, "acct-green", "100", "2026-09-04T18:25:00Z")  # 300 s before NOW

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert (
        "account lane=frontend-hotspots account=acct-green five_hour_pct=42 age_s=300 "
        "gate=deny (seven_day_pct=100 >=90: the weekly window is spent" in out
    )
    assert "account=acct-green five_hour_pct=42 age_s=300 gate=allow" not in out


def test_a_weekly_reading_below_the_mark_or_stale_leaves_allow_standing(tmp_path):
    """CONTROL for the weekly deny: if a fresh 89 or a stale 100 turns the lane
    red then broken. The overshoot would refuse every lane whose weekly meter
    stopped updating, which is the five-hour rail's job to name, not this one's."""
    for pct, stamp in (("89", "2026-09-04T18:25:00Z"), ("100", "2026-09-04T17:48:20Z")):
        fixture = _copy_fixture(tmp_path / pct)
        _write_seven_day(fixture, "acct-green", pct, stamp)
        out = _run(_env(fixture, _home(tmp_path / pct, token_profile=True))).stdout
        assert (
            "account lane=frontend-hotspots account=acct-green five_hour_pct=42 "
            "age_s=300 gate=allow" in out
        ), (pct, stamp)


def test_flip_gate_refuses_when_account_rotation_is_absent_entirely(tmp_path):
    """If the whole rotation file is missing then broken the same way as an empty one."""
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "state" / "account-rotation").unlink()

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert "account lane=ci-infra account=acct-hot five_hour_pct=91 age_s=300 gate=deny" in out
    assert "gate=flip" not in out


def test_lane_accounts_present_but_empty_reports_a_broken_map_not_silence(tmp_path):
    """If lane-accounts holds only comments/blanks and the rail goes silent then broken.

    An absent file already reports gate=unmeasurable (test above); a present
    but empty one must say the same thing, not simply print nothing.
    """
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "state" / "lane-accounts").write_text(
        "# nothing declared\n\n# still nothing\n"
    )

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert (
        "account lane=none account=none five_hour_pct=UNKNOWN age_s=none "
        "gate=unmeasurable (state/lane-accounts present but declares no "
        "lane->account rows: a broken map, not zero lanes)" in out
    )
    assert "gate=allow" not in out
    assert "gate=flip" not in out


def test_lane_accounts_last_row_without_a_trailing_newline_is_not_dropped(tmp_path):
    """If a final row with no trailing newline is silently skipped then broken.

    Plain `while read` treats that read's EOF-without-newline status as
    loop-false, so the last lane's gate line vanishes with no error at all.
    """
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "state" / "lane-accounts").write_bytes(
        b"frontend-hotspots acct-green\nbackend acct-warm"
    )

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert "account lane=frontend-hotspots account=acct-green five_hour_pct=42 age_s=300 gate=allow" in out
    assert "account lane=backend account=acct-warm five_hour_pct=87 age_s=300 gate=deny" in out


def test_duplicate_fix_reports_unmeasurable_when_the_closed_pr_read_is_capped(tmp_path):
    """If a capped closed-PR read prints its truncated count as measured then broken.

    The committed closed.json fixture has exactly 4 rows; pointing
    KPI_CLOSED_PR_LIMIT at 4 makes that read look capped without needing a
    100-row fixture, and the line must refuse the count rather than print it.
    """
    env = _env(_copy_fixture(tmp_path), _home(tmp_path, token_profile=True))
    env["KPI_CLOSED_PR_LIMIT"] = "4"

    out = _run(env).stdout
    assert (
        "duplicate-fix superseded_prs_today=unmeasurable prs=[] (closed-PR "
        "read hit row cap 4 before reaching 2026-09-04T00:00:00Z; a capped read is not a full day's "
        "count, refusing rather than reporting a truncated number as "
        "measured)" in out
    )
    assert "superseded_prs_today=2" not in out


def test_duplicate_fix_measures_normally_when_the_read_is_under_the_cap(tmp_path):
    """If raising the cap above the fixture size still refuses to measure then broken."""
    env = _env(_copy_fixture(tmp_path), _home(tmp_path, token_profile=True))
    env["KPI_CLOSED_PR_LIMIT"] = "5"

    out = _run(env).stdout
    assert "duplicate-fix superseded_prs_today=2 prs=[1306,1327]" in out


def test_an_unparseable_five_hour_stamp_reaches_its_own_named_branch(tmp_path):
    """If a garbage -measured-at stamp aborts the script instead of reaching
    the 'unparseable reading' branch then broken.

    This script only sets `set -u` (never `set -e`/`pipefail`), so a bare
    `_epoch=$(date -u -d "$_when" +%s 2>/dev/null)` whose command fails just
    leaves `_epoch` empty; it does not exit the script. Proved two ways: the
    lane's own line names the unparseable input, and a section that runs
    strictly LATER in the script (self-merge) still printed at all.
    """
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "state" / "acct-green-five-hour-measured-at").write_text(
        "not-a-timestamp\n"
    )

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert (
        "account lane=frontend-hotspots account=acct-green five_hour_pct=UNKNOWN "
        "age_s=none gate=deny (unparseable reading in "
        "state/acct-green-five-hour-pct or -measured-at)" in out
    )
    assert "self-merge rate=" in out  # proves the script ran past this line


def test_a_corrupt_closed_pr_payload_reaches_its_own_named_branch(tmp_path):
    """If non-JSON closed-PR fixture content aborts the script instead of
    reaching the 'closed-PR JSON did not parse' branch then broken.

    Same premise as the test above: no `set -e`/`pipefail` is active, so a
    `jq` parse failure on bad input just empties the variable it feeds. Proved
    the same two ways: the duplicate-fix line names the parse failure, and the
    LATER workers section still printed.
    """
    fixture = _copy_fixture(tmp_path)
    (fixture / "gh" / "closed.json").write_text("not valid json at all }{\n")

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert (
        "duplicate-fix superseded_prs_today=unmeasurable prs=[] (closed-PR "
        "JSON did not parse; refusing a silent 0)" in out
    )
    assert "workers attempts_started=" in out  # proves the script ran past this line
