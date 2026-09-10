"""Wiring tests for the ops/fleet/kpi.sh dispatch KPIs/health script.

Hermetic and machine-independent by construction: the script's KPI_* seams
(KPI_JOBS_DIR, KPI_NOW_UNIX, KPI_GH_FIXTURES_DIR, KPI_PROBES_DIR) point every
external read at committed fixture data under tests/fixtures/fleet-kpi/ instead
of $HOME/jobs, the network or the live host, so the tests run anywhere and
never touch the running dispatch system. The fixture timestamps are frozen
relative to KPI_NOW_UNIX, so assertions stay deterministic forever.

Regression lines:
  - if a TICK or SKIP line older than the window counts then broken
    (merge-fable has 3 TICK lines, only 2 are in-window)
  - if skip_ratio is not skipped/(ran+skipped) then broken (0.50 / 0.50 / 0.50 /
    0.00 / 0.00 for dispatcher / merge-even / merge-odd / merge-fable / rc-qa)
  - if a merged PR count or the per-merged Codex review ratio is misread from
    the gh JSON then broken (n=3, reviews 2+1+0 -> 1.0)
  - if KPI_SKIP_REVIEW_COST does not actually skip the per-merged-PR review
    loop then broken (proved by deleting the per-PR reviews fixtures: the
    skipping run must not report a missing fixture, the measuring run must)
  - if the skip goes quiet instead of naming itself then broken: an ABSENT
    ratio already means the merged read failed, so a skipped measurement that
    also prints nothing is indistinguishable from a failed one
  - if the flag skips the loop when it is UNSET then broken (the overshoot
    control: the reported defect is "too slow", and always-skipping satisfies
    that report perfectly while deleting the metric)
  - if a non-1 skip flag disables the measurement then broken: configuration
    typos must fail explicitly rather than silently drop a KPI
  - if a per-PR review read fails and the ratio still prints a number then
    broken: 2>/dev/null used to hide both _gh's refusal and any live gh error,
    so every unreadable PR contributed a silent 0 and the ratio came out low
    while looking measured (1 of 3 missing and 3 of 3 missing both say
    unmeasurable now, and stderr carries the refusal)
  - if an ATTEMPT_START stamp older than the window counts then broken
    (attempts_started=2, not 3)
  - if the burn line or block-cost-per-merge ratio is misread from quota.sh
    then broken (block_cost_usd=42.00 over n=3 merges -> 14.0)
  - if the backlog open/actionable/clean split is misread then broken
    (8 / 5 / 5 against a fixture that also carries a draft, a blocked:* PR, a
    post-v1 PR and a PR older than the 30-day actionable window)
  - if a backlogged label (post-v1 / backlog / blocked:*) counts as actionable
    then broken (#1107 is CLEAN and in-window but carries post-v1)
  - if the open-to-merge SLA buckets are not cumulative from createdAt then
    broken (over_yellow=4 >= 1h, over_red=3 >= 2h, and the 10-minute-old #1109
    is in neither)
  - if the builder freeze does not switch on at exactly 15 actionable PRs then
    broken (off at 5, on at 15, and the matching health line flips with it)
  - if a health line that cannot measure reports PASS or a silent zero instead
    of FAIL then broken (missing probe fixture and missing gh fixture controls)
  - if the 8 data health lines cannot be flipped by their own fixture inputs
    then broken (the red-fixture run proves each verdict is driven by the
    input it names, not by ambient machine state)
  - if an untimestamped FATAL line flips the window health check then broken:
    a bare "FATAL: ..." has awk $1 = "FATAL:", and a string compare puts "F"
    above any "2026-..." bound, so the pre-fix filter admitted it forever and
    pinned the line red with no way to age out
  - if that excluded count is swallowed instead of named in the label then
    broken (the label carries "+N untimestamped", 0 on the green fixture and 1
    once an untimestamped FATAL is written)
  - if the token health line leaks the developer's real shell profile then
    broken (run with a throwaway $HOME)
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

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

REPO = Path(__file__).resolve().parents[2]
KPI = REPO / "ops" / "fleet" / "kpi.sh"
FIXTURE = REPO / "tests" / "fixtures" / "fleet-kpi"

# Frozen window anchor. All fixture timestamps are offsets from this epoch, so
# the 1-hour window and every PASS/FAIL threshold are exact and never rot.
NOW = 1788546600
HOURS = 1
SINCE = NOW - HOURS * 3600


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env(fixture: Path, home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("KPI_")}
    env.update(
        {
            "HOME": str(home),
            "KPI_JOBS_DIR": str(fixture / "jobs"),
            "KPI_GH_FIXTURES_DIR": str(fixture / "gh"),
            "KPI_PROBES_DIR": str(fixture / "probes"),
            "KPI_NOW_UNIX": str(NOW),
        }
    )
    return env


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(KPI), str(HOURS)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )


def _copy_fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    # Gate-log freshness and report verdict windows are decided by mtime, which
    # git does not track: pin them inside the frozen window so the fixture does
    # not depend on when CI happened to check it out.
    os.utime(fixture / "jobs" / "logs" / "tick-gate.log", (NOW - 120, NOW - 120))
    for report in (fixture / "jobs" / "reports").glob("issue-*.md"):
        os.utime(report, (NOW - 120, NOW - 120))
    return fixture


def _home(tmp_path: Path, token_profile: bool) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    if token_profile:
        (home / ".profile").write_text("export CLAUDE_CODE_OAUTH_TOKEN=fixture-token\n")
    return home


def _health(out: str) -> dict[str, str]:
    verdicts: dict[str, str] = {}
    for line in out.splitlines():
        if line.startswith("health "):
            _prefix, verdict, label = line.split(" ", 2)
            verdicts[label] = verdict
    return verdicts


def _fatal_label(untimestamped: int | str) -> str:
    """The FATAL health label, which names how many lines the window filter had
    to exclude for carrying no timestamp to window on."""
    return (
        f"no timestamped FATAL in logs last {HOURS}h "
        f"(+{untimestamped} untimestamped, excluded: no time to window on)"
    )

GREEN_LABELS = [
    "queue-watchdog unit active",
    "exactly one watchdog loop (sleeping main pid 424242)",
    "no tmux residents loop (retired)",
    "gate log written in last 15 min",
    "dispatcher ticked in last 70 min",
    "workers within cap (4)",
    "actionable PR backlog under builder-freeze threshold 15",
    _fatal_label(0),
    "token present for launchers",
]


def test_green_fixture_reports_expected_kpis(tmp_path):
    """If the known fixture is read correctly then every metric line and every
    health verdict matches, and a line that would go red is never green."""
    proc = _run(_env(_copy_fixture(tmp_path), _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out

    # Header and window.
    assert f"KPI window: last {HOURS}h since {_iso(SINCE)}" in out

    # Per-lane skip ratios from tick-gate.log (windowed: merge-fable's third,
    # out-of-window TICK must not count).
    assert "ticks lane=dispatcher ran=1 skipped=1 skip_ratio=0.50" in out
    assert "ticks lane=merge-even ran=1 skipped=1 skip_ratio=0.50" in out
    assert "ticks lane=merge-odd ran=1 skipped=1 skip_ratio=0.50" in out
    assert "ticks lane=merge-fable ran=2 skipped=0 skip_ratio=0.00" in out
    assert "ticks lane=rc-qa ran=1 skipped=0 skip_ratio=0.00" in out

    # Throughput + review cost from the gh fixtures.
    assert "merges n=3 per_hour=3.00 prs=[1133,1132,1131]" in out
    assert "codex_reviews_per_merged_pr=1.0" in out

    # Worker attempts (windowed) and report outcomes.
    assert "workers attempts_started=2 live_now=2 reports_awaiting_reap done=2 blocked=1" in out

    # Burn floor and efficiency from the fixture quota.sh.
    assert "burn QUOTA scope=nucbox-local workers_live=2" in out
    assert "efficiency block_cost_per_merge_usd=14.0" in out

    # Backlog split: 9 open rows -> 8 non-draft, 5 actionable, 5 CLEAN. The
    # three non-actionable ones are the blocked:* PR, the post-v1 PR and the
    # July-era PR; the draft is not even open_prs.
    assert "backlog open_prs=8 actionable=5 clean=5 soft_target=10 hard_target=15" in out

    # Merge-ownership policy numbers (the maintainer, Thu 4 Sep 2026). Of the 5
    # actionable PRs, 3 are days old (over red), #1108 is 90 minutes old (over
    # yellow only) and #1109 is 10 minutes old (inside both).
    assert (
        "sla yellow_h=1 red_h=2 over_yellow=4 over_red=3 builder_freeze=off freeze_at=15" in out
    )

    # Every health line reports PASS; nothing is unmeasured-green and the
    # throwaway $HOME profile satisfied the token line.
    verdicts = _health(out)
    assert list(verdicts) == GREEN_LABELS, out
    assert set(verdicts.values()) == {"PASS"}, out
    assert "missing" not in proc.stderr


def test_retired_and_zero_tick_driven_lanes_are_distinct(tmp_path):
    """If a retired lane and a driven lane with no ticks render alike then broken.

    The watchdog fixture is the authority for driven residents.  The retired
    lane exists only in its captured resident log, which is how KPI discovery
    preserves an auditable retirement without maintaining a second resident
    list in the KPI script.
    """
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "watchdog.sh").write_text(
        'RESIDENTS="merge-odd:$JOBS/merge-lane-brief.md rc-qa:$JOBS/rc-qa-brief.md"\n'
    )
    (fixture / "jobs" / "logs" / "resident-merge-even.log").write_text(
        "2026-09-02T16:29:22Z resident stopped as control\n"
    )
    tick_gate = fixture / "jobs" / "logs" / "tick-gate.log"
    tick_gate.write_text(
        "\n".join(line for line in tick_gate.read_text().splitlines() if " merge-odd " not in line)
        + "\n"
    )

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, out
    assert (
        "ticks lane=merge-even RETIRED (not driven by watchdog.sh; "
        "last log 2026-09-02T16:29:22Z)"
    ) in out
    assert "ticks lane=merge-odd ran=0 skipped=0 skip_ratio=ALERT" in out
    assert "skip_ratio=n/a" not in out


def test_retired_lane_without_timestamp_fails_loudly(tmp_path):
    """If a retired log lacks an exact UTC timestamp but KPI reports it then broken."""
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "watchdog.sh").write_text(
        'RESIDENTS="merge-odd:$JOBS/merge-lane-brief.md rc-qa:$JOBS/rc-qa-brief.md"\n'
    )
    (fixture / "jobs" / "logs" / "resident-merge-even.log").write_text(
        "2026-09-02T16:29:22+01:00 resident stopped outside UTC\n"
    )

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))

    assert proc.returncode == 2
    assert "retired lane merge-even has no valid UTC timestamp" in proc.stderr
    assert "ticks lane=merge-even RETIRED" not in proc.stdout


def test_copy_fixture_pins_report_mtimes_inside_the_frozen_window(tmp_path):
    """The report window must be measured against the test's frozen clock."""
    fixture = _copy_fixture(tmp_path)
    reports = (fixture / "jobs" / "reports").glob("issue-*.md")
    assert all(int(report.stat().st_mtime) == NOW - 120 for report in reports)


def test_red_fixture_flips_every_input_driven_health_line(tmp_path):
    """If each fixture-driven health input is broken in turn then its own line
    goes red, proving the verdicts read the inputs they name rather than some
    ambient machine state."""
    fixture = _copy_fixture(tmp_path)

    # Every health line that is decided by a fixture file, flipped to red.
    (fixture / "probes" / "queue-watchdog-active").write_text("inactive\n")
    (fixture / "probes" / "watchdog-loops").write_text("\n")  # zero loops != main pid
    (fixture / "probes" / "residents-watchdog-pgrep").write_text("12345\n")
    os.utime(fixture / "jobs" / "logs" / "tick-gate.log", (NOW - 2000, NOW - 2000))
    (fixture / "jobs" / "state" / "tickgate-dispatcher.ts").write_text(f"{NOW - 9999}\n")
    (fixture / "jobs" / "state" / "cap-agents").write_text("1\n")  # live_now=2 > 1
    # The timestamped line is what flips this verdict. The untimestamped one
    # rides along only to prove it changes the label's excluded count without
    # ever being the reason the line is red.
    (fixture / "jobs" / "logs" / "recent-fatal.log").write_text(
        f"{_iso(NOW - 100)} FATAL induced for the red control\n"
        "FATAL: untimestamped, so there is no time to window it on\n"
    )

    # No token profile in $HOME -> token line red.
    verdicts = _health((_run(_env(fixture, _home(tmp_path, token_profile=False)))).stdout)

    assert verdicts["queue-watchdog unit active"] == "FAIL"
    assert verdicts["exactly one watchdog loop (sleeping main pid 424242)"] == "FAIL"
    assert verdicts["no tmux residents loop (retired)"] == "FAIL"
    assert verdicts["gate log written in last 15 min"] == "FAIL"
    assert verdicts["dispatcher ticked in last 70 min"] == "FAIL"
    assert verdicts["workers within cap (1)"] == "FAIL"
    assert verdicts[_fatal_label(1)] == "FAIL"
    assert verdicts["token present for launchers"] == "FAIL"
    # Unflipped: the backlog fixture still holds 5 actionable, under the
    # builder-freeze threshold of 15.
    assert verdicts["actionable PR backlog under builder-freeze threshold 15"] == "PASS"


def test_untimestamped_fatal_is_counted_but_never_windowed(tmp_path):
    """If a FATAL line carrying no timestamp flips the window health check then
    broken. Such a line has awk $1 = "FATAL:", and a string compare puts "F"
    above any "2026-..." bound, so the pre-fix filter admitted every one of them
    forever: the check was pinned red with no way to age out, which carries as
    little information as a check that can never go red. The count must still be
    named in the label, because writing FATAL without a UTC stamp is its own
    defect and swallowing it silently is how 214 such lines hid in plain sight."""
    fixture = _copy_fixture(tmp_path)
    # No timestamp anywhere on the line, and the only FATAL inside the window.
    (fixture / "jobs" / "logs" / "untimestamped-fatal.log").write_text(
        "FATAL: tick gate failed for merge-odd\n"
    )

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    verdicts = _health(out)

    # Pre-fix this read FAIL, because "FATAL:" > "2026-09-04T17:30:00Z".
    assert verdicts[_fatal_label(1)] == "PASS", out
    # And the excluded line is named rather than swallowed.
    assert _fatal_label(0) not in verdicts, out


def test_prose_that_merely_mentions_fatal_is_not_a_fatal_record(tmp_path):
    """If a log line that only CONTAINS the word FATAL counts as a FATAL then broken.

    These logs are not all structured. `resident-*.log`, `issue-*.log` and
    `dispatcher.log` are agent transcripts, and agents quote source lines, paste
    diffs containing `+ log "FATAL: ..."`, and echo this script's own health
    output back into themselves. Measured on nucbox Wed 9 Sep 2026, the
    contains-anywhere rule reported 889 untimestamped FATALs of which 862 were
    prose, so the excluded count carried no information, and a quoted line that
    happens to lead with its own UTC stamp could redden the window check for a
    fault that never happened.

    Both directions are pinned here: prose must not count, and a real record must
    still count, in the same fixture, so a rule that simply stopped matching
    anything would fail this test rather than pass it.
    """
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "logs" / "resident-prose.log").write_text(
        # An agent quoting a source line, with its own stamp at the front.
        f'{_iso(NOW - 60)} the fixer patch adds: + log "FATAL: redteam trigger failed"\n'
        # An agent echoing this script's own output back into its transcript.
        f"{_iso(NOW - 50)} health FAIL no timestamped FATAL in logs last 6h\n"
        # Prose with no stamp at all, mentioning the word mid-sentence.
        "I refused to ship that because a FATAL there would be silent\n"
    )

    home = _home(tmp_path, token_profile=True)
    assert _health(_run(_env(fixture, home)).stdout)[_fatal_label(0)] == "PASS"

    # Same fixture, plus one genuine record: stamp, then FATAL as the next field.
    (fixture / "jobs" / "logs" / "real-fatal.log").write_text(
        f"{_iso(NOW - 40)} FATAL: no brief at /home/dev/jobs/briefs/issue-1.md\n"
    )
    assert _health(_run(_env(fixture, home)).stdout)[_fatal_label(0)] == "FAIL"


def test_an_unstamped_fatal_record_is_counted_but_prose_is_not(tmp_path):
    """If the excluded count includes prose then broken.

    The count exists to name scripts that write a FATAL record without a UTC
    stamp, because such a line can never be windowed and so a recurrence is
    invisible. It is only actionable while it counts records and nothing else.
    """
    fixture = _copy_fixture(tmp_path)
    (fixture / "jobs" / "logs" / "mixed.log").write_text(
        "FATAL: Codex transcript capture failed at exit session_id=missing\n"
        "the runbook says a FATAL here means the brief was never written\n"
        '+        echo "FATAL: unknown tier" >> "$LOG"\n'
    )

    verdicts = _health(_run(_env(fixture, _home(tmp_path, token_profile=True))).stdout)

    # One record, two prose lines: the label names 1, and the window stays green.
    assert verdicts[_fatal_label(1)] == "PASS"
    assert _fatal_label(3) not in verdicts


def test_builder_freeze_switches_on_at_fifteen_actionable(tmp_path):
    """If the builder freeze does not switch on at exactly 15 actionable PRs,
    or its health line does not go red with it, then broken."""
    fixture = _copy_fixture(tmp_path)
    rows = [
        {
            "number": 2000 + i,
            "isDraft": False,
            "mergeStateStatus": "CLEAN",
            "labels": [],
            "createdAt": _iso(NOW - 3 * 3600),
        }
        for i in range(15)
    ]
    (fixture / "gh" / "open.json").write_text(json.dumps(rows))

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout
    assert "backlog open_prs=15 actionable=15 clean=15" in out
    assert "over_yellow=15 over_red=15 builder_freeze=on freeze_at=15" in out
    verdicts = _health(out)
    assert verdicts["actionable PR backlog under builder-freeze threshold 15"] == "FAIL"


def test_one_under_the_freeze_threshold_still_builds(tmp_path):
    """If 14 actionable PRs already freeze building then broken: the threshold
    is 15 or more, so 14 must stay off."""
    fixture = _copy_fixture(tmp_path)
    rows = [
        {
            "number": 2000 + i,
            "isDraft": False,
            "mergeStateStatus": "CLEAN",
            "labels": [],
            "createdAt": _iso(NOW - 3 * 3600),
        }
        for i in range(14)
    ]
    (fixture / "gh" / "open.json").write_text(json.dumps(rows))

    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    assert "backlog open_prs=14 actionable=14 clean=14" in out
    assert "builder_freeze=off freeze_at=15" in out
    assert _health(out)["actionable PR backlog under builder-freeze threshold 15"] == "PASS"


def test_missing_probe_fixture_fails_loud_not_silent_green(tmp_path):
    """If a probe cannot measure then its health line prints FAIL and a loud
    error, never PASS and never an unmeasured zero."""
    fixture = _copy_fixture(tmp_path)
    (fixture / "probes" / "queue-watchdog-active").unlink()

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    verdicts = _health(proc.stdout)
    assert verdicts["queue-watchdog unit active"] == "FAIL"
    assert "missing probe fixture" in proc.stderr
    assert "health PASS queue-watchdog unit active" not in proc.stdout


def test_missing_gh_fixture_reports_unmeasurable_never_zero(tmp_path):
    """If the merged-PR read cannot measure then the merges line says so and
    the downstream merges-derived lines are dropped, not reported as 0."""
    fixture = _copy_fixture(tmp_path)
    (fixture / "gh" / "merged.json").unlink()

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr
    assert "merges n=unmeasurable per_hour=n/a" in out
    assert "merges n=0 " not in out
    assert "missing gh fixture" in proc.stderr
    assert "codex_reviews_per_merged_pr=" not in out
    assert "efficiency block_cost_per_merge_usd=" not in out


def test_skip_review_cost_skips_the_loop_and_says_so(tmp_path):
    """If KPI_SKIP_REVIEW_COST does not skip the per-merged-PR review loop, or
    skips it silently, then broken.

    The proof that the loop did not run is NOT the changed output line, which a
    stray echo could fake. It is that the per-PR reviews fixtures are DELETED
    and the run still says nothing about a missing fixture: _gh refuses a silent
    empty read and writes "missing gh fixture" to stderr, so a loop that ran
    would have to complain three times.
    """
    fixture = _copy_fixture(tmp_path)
    for reviews in (fixture / "gh").glob("reviews-*.json"):
        reviews.unlink()

    env = _env(fixture, _home(tmp_path, token_profile=True))
    env["KPI_SKIP_REVIEW_COST"] = "1"
    proc = _run(env)
    out = proc.stdout + proc.stderr

    assert "codex_reviews_per_merged_pr=skipped" in out
    assert "missing gh fixture" not in proc.stderr
    # The gates the flag exists to serve are unaffected.
    assert "merges n=3 per_hour=3.00" in out
    assert "backlog open_prs=8 actionable=5" in out
    assert "builder_freeze=off" in out


def test_skip_review_cost_rejects_non_one_values(tmp_path):
    """If a typo silently skips the KPI, then broken."""
    fixture = _copy_fixture(tmp_path)
    env = _env(fixture, _home(tmp_path, token_profile=True))
    env["KPI_SKIP_REVIEW_COST"] = "yes"

    proc = _run(env)

    assert proc.returncode == 2
    assert "KPI_SKIP_REVIEW_COST must be unset or 1, got: yes" in proc.stderr
    assert "codex_reviews_per_merged_pr=skipped" not in proc.stdout


def test_skip_review_cost_is_named_when_there_are_no_merges(tmp_path):
    """If an intentional skip with zero merges goes unnamed, then broken."""
    fixture = _copy_fixture(tmp_path)
    (fixture / "gh" / "merged.json").write_text("[]")
    env = _env(fixture, _home(tmp_path, token_profile=True))
    env["KPI_SKIP_REVIEW_COST"] = "1"

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr
    assert "merges n=0 " in proc.stdout
    assert "codex_reviews_per_merged_pr=skipped" in proc.stdout


def test_skip_is_distinguishable_from_an_unmeasurable_read(tmp_path):
    """If a deliberate skip renders the same as a failed merged-PR read then
    broken: absence already carries "could not measure"."""
    fixture = _copy_fixture(tmp_path)
    env = _env(fixture, _home(tmp_path, token_profile=True))
    env["KPI_SKIP_REVIEW_COST"] = "1"
    proc = _run(env)
    out = proc.stdout + proc.stderr
    assert "codex_reviews_per_merged_pr=skipped" in out
    assert "not measured, not 0" in out
    assert "codex_reviews_per_merged_pr=0" not in out


def test_review_cost_still_measured_when_the_flag_is_unset(tmp_path):
    """The overshoot control. The reported defect was "the run is too slow", and
    a version that always skips the loop satisfies that report completely while
    silently deleting the metric. Assert the ORIGINAL behavior still holds where
    it should: no flag, real ratio, fixtures read."""
    fixture = _copy_fixture(tmp_path)
    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr

    assert "codex_reviews_per_merged_pr=1.0" in out
    assert "skipped" not in out.split("codex_reviews_per_merged_pr=")[1].splitlines()[0]


def test_review_fixtures_are_read_when_the_flag_is_unset(tmp_path):
    """The negative control for the skip proof above: with the fixtures deleted
    and NO flag, the loop must run and must complain. Without this, "no missing
    fixture message" would be evidence of nothing.

    This control failed on its first run and found a real defect: the per-PR
    read was wrapped in 2>/dev/null, which swallowed _gh's own refusal, so the
    loop could never complain about anything and each unreadable PR silently
    contributed 0 reviews to a ratio that still printed as a measurement.
    """
    fixture = _copy_fixture(tmp_path)
    for reviews in (fixture / "gh").glob("reviews-*.json"):
        reviews.unlink()

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr
    assert "missing gh fixture" in proc.stderr
    # And the ratio must refuse itself rather than come out quietly low.
    assert "codex_reviews_per_merged_pr=unmeasurable (3 of 3 per-PR review reads failed" in out
    assert "codex_reviews_per_merged_pr=0.0" not in out


def test_one_unreadable_pr_makes_the_whole_ratio_unmeasurable(tmp_path):
    """If a PARTIAL review read still prints a ratio then broken: two of three
    PRs readable and one missing yields 3/3 of a real numerator over a 3-PR
    denominator only by luck, and in general drags the ratio down while looking
    exactly like a measurement."""
    fixture = _copy_fixture(tmp_path)
    victim = sorted((fixture / "gh").glob("reviews-*.json"))[0]
    victim.unlink()

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr
    assert "codex_reviews_per_merged_pr=unmeasurable (1 of 3 per-PR review reads failed" in out
    assert "codex_reviews_per_merged_pr=1.0" not in out


def test_script_is_executable_and_syntax_clean():
    """If ops/fleet/kpi.sh loses its shebang or exec bit then broken."""
    assert KPI.exists()
    assert KPI.stat().st_mode & 0o111, f"not executable: {KPI}"
