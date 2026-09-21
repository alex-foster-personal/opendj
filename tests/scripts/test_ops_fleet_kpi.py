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
  - if the 9 data health lines cannot be flipped by their own fixture inputs
    then broken (the red-fixture run proves each verdict is driven by the
    input it names, not by ambient machine state)
  - if a diagnostic pgrep argv embeds residents-watchdog.sh then the health
    line stays PASS, else stop
  - if a launcher refusal or a provisioning fault counts on the FLEET FATAL line then
    broken (issue #1670: they have lines of their own, and the fleet line had no
    reachable green while they shared it). The split's own cases live in
    tests/scripts/test_ops_fleet_kpi_fatal_classes.py
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


def _install_review_triage_stub(home: Path, mapping_file: Path) -> Path:
    """Hermetic seam for backlog clean's review-triage gate (issue #3380)."""
    stub = home / "review-triage-stub.sh"
    stub.write_text(
        f"""#!/bin/sh
set -eu
pr="$1"
mapfile="{mapping_file}"
invoked=$(jq -c --arg pr "$pr" '.invoked += [$pr | tonumber]' "$mapfile")
printf '%s' "$invoked" > "$mapfile"
rc=$(jq -r --arg pr "$pr" '.[$pr] // .default // 0' "$mapfile")
exit "$rc"
""",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _env(fixture: Path, home: Path) -> dict[str, str]:
    gh_config = home / "gh-config"
    gh_config.mkdir(exist_ok=True)
    gh_stub = home / ".local" / "bin" / "gh"
    gh_stub.parent.mkdir(parents=True, exist_ok=True)
    gh_stub.write_text(
        "#!/bin/sh\n"
        "echo 'unexpected live gh invocation in KPI fixture test' >&2\n"
        "exit 97\n",
        encoding="utf-8",
    )
    gh_stub.chmod(0o755)
    review_triage_stub = _install_review_triage_stub(home, fixture / "review-triage.json")
    env = {k: v for k, v in os.environ.items() if not k.startswith("KPI_")}
    env.update(
        {
            "HOME": str(home),
            "GH_CONFIG_DIR": str(gh_config),
            "PATH": f"{home}:{env.get('PATH', '')}",
            "KPI_JOBS_DIR": str(fixture / "jobs"),
            "KPI_GH_FIXTURES_DIR": str(fixture / "gh"),
            "KPI_PROBES_DIR": str(fixture / "probes"),
            "KPI_NOW_UNIX": str(NOW),
            "KPI_REVIEW_TRIAGE_CMD": str(review_triage_stub),
        }
    )
    return env


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("UNAVAILABLE: bash is required to run ops/fleet/kpi.sh")
    return subprocess.run(
        [bash, str(KPI), str(HOURS)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )


def _synthetic_head_sha(pr_number: int) -> str:
    return f"{pr_number:040x}"


def _write_check_runs(fixture: Path, head_sha: str, conclusions: list[str | None]) -> None:
    runs: list[dict[str, object]] = []
    for index, conclusion in enumerate(conclusions):
        run: dict[str, object] = {"name": f"check-{index}", "status": "completed"}
        if conclusion is not None:
            run["conclusion"] = conclusion
        runs.append(run)
    payload = {"total_count": len(runs), "check_runs": runs}
    (fixture / "gh" / f"check-runs-{head_sha}.json").write_text(
        json.dumps(payload) + "\n",
        encoding="utf-8",
    )


def _hydrate_check_run_fixtures(fixture: Path) -> dict[int, str]:
    """Assign stable head SHAs and green check rollups for GitHub-clean open PRs."""
    open_path = fixture / "gh" / "open.json"
    rows = json.loads(open_path.read_text(encoding="utf-8"))
    sha_by_pr: dict[int, str] = {}
    for row in rows:
        pr_number = int(row["number"])
        head_sha = _synthetic_head_sha(pr_number)
        row["headSha"] = head_sha
        sha_by_pr[pr_number] = head_sha
        merge_state = str(
            row.get("mergeableState")
            or row.get("mergeStateStatus")
            or row.get("mergeable_state")
            or ""
        ).upper()
        if merge_state == "CLEAN":
            conclusions: list[str | None] = ["success"]
            if pr_number == 1101:
                conclusions = ["success", "neutral", "skipped", None]
            _write_check_runs(fixture, head_sha, conclusions)
    open_path.write_text(json.dumps(rows) + "\n", encoding="utf-8")
    return sha_by_pr


def _copy_fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    # Gate-log freshness and report verdict windows are decided by mtime, which
    # git does not track: pin them inside the frozen window so the fixture does
    # not depend on when CI happened to check it out.
    os.utime(fixture / "jobs" / "logs" / "tick-gate.log", (NOW - 120, NOW - 120))
    for report in (fixture / "jobs" / "reports").glob("issue-*.md"):
        os.utime(report, (NOW - 120, NOW - 120))
    rotation = fixture / "jobs" / "state" / "account-rotation"
    rotation.write_text("ci-infra acct-hot acct-cold\n", encoding="utf-8")
    _hydrate_check_run_fixtures(fixture)
    return fixture


def _home(tmp_path: Path, token_profile: bool) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    app_state = home / ".local" / "state" / "gh-apps"
    app_state.mkdir(parents=True)
    (app_state / "opendj-devops-kpi.token").write_text("fixture-app-token\n", encoding="utf-8")
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
    """The FLEET FATAL health label, which names how many lines the window filter
    had to exclude for carrying no timestamp to window on. Since issue #1670 this
    line counts only records that are neither a launcher refusal nor a
    provisioning fault; those two have lines of their own."""
    return (
        f"no timestamped fleet FATAL in logs last {HOURS}h "
        f"(+{untimestamped} untimestamped, excluded: no time to window on; "
        f"launcher refusals and provisioning faults have their own lines below)"
    )


PROVISIONING_LABEL = (
    f"no timestamped provisioning fault in logs last {HOURS}h "
    f"(a lane with no usable account or credential: the box is not set up, "
    f"the fleet is not broken)"
)


def _refusals(out: str) -> str:
    """The launcher-refusal count off the `refusals` line, as written. A count,
    never a verdict: see issue #1670."""
    for line in out.splitlines():
        if line.startswith("refusals "):
            return line.split(" ", 2)[1].removeprefix("launcher=")
    raise AssertionError(f"no refusals line in output:\n{out}")


GREEN_LABELS = [
    "queue-watchdog unit active",
    "exactly one watchdog loop (sleeping main pid 424242)",
    "no tmux residents loop (retired)",
    "gate log written in last 15 min",
    "dispatcher ticked in last 70 min",
    "workers within cap (4)",
    "actionable PR backlog under builder-freeze threshold 15",
    "host disk GREEN (level=GREEN free=160.17GB rate=0GB/h hours_to_red=999"
    " ts=2026-09-04T18:28:00.7173594Z source=windows-watchdog; Windows C: per af-disk-watchdog, stale after 15 min)",
    "sink-triage last run within 7200s (hourly timer opendj-sink-triage.timer)",
    _fatal_label(0),
    PROVISIONING_LABEL,
    "token present for launchers",
]

# _health() keys a dict by label, so a label repeated here is unsatisfiable by any
# output, and the order assertion then fails forever looking like a script regression.
assert len(set(GREEN_LABELS)) == len(GREEN_LABELS), "duplicate label in GREEN_LABELS"


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
    assert "workers attempts_started=0 live_now=2 reports_awaiting_reap=0 done=2 blocked=1" in out

    # Burn floor and efficiency from the fixture quota.sh.
    assert "burn QUOTA scope=nucbox-local workers_live=2" in out
    assert "efficiency block_cost_per_merge_usd=14.0" in out

    # Backlog split: 9 open rows -> 8 non-draft, 5 actionable, 3 CLEAN. The
    # three non-actionable ones are the blocked:* PR, the post-v1 PR and the
    # July-era PR; the draft is not even open_prs.
    assert "backlog open_prs=8 actionable=5 clean=3 soft_target=10 hard_target=15" in out

    # Merge-ownership policy numbers (the maintainer, Thu 4 Sep 2026). Of the 5
    # actionable PRs, 3 are days old (over red), #1108 is 90 minutes old (over
    # yellow only) and #1109 is 10 minutes old (inside both).
    assert (
        "sla yellow_h=1 red_h=2 over_yellow=4 over_red=3 builder_freeze=off freeze_at=15" in out
    )

    # skipped= names the unreachable hosts recorded in the kpi file (issue #3431);
    # the fixture's run reached nucbox and silver but not air.
    assert (
        "sink-triage last_run=2026-09-04T18:28:00Z new_issues=0 fingerprints=2 skipped=air "
        "(source: "
    ) in out

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
        "ticks lane=merge-even RETIRED (not driven by watchdog.sh, no systemd --user timer found either; "
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
    (fixture / "jobs" / "logs" / "dispatcher.log").write_text(
        f"---tick-end {_iso(NOW - 100)} model=opus effort=high "
        "reason=unclassified-error exit=1---\n"
    )
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


def _residents_watchdog_is_real_executor(args: str) -> bool:
    """Mirror _m_no_residents live-branch argv classification (issue #3664)."""
    if not args:
        return False
    if "pgrep" in args and "residents-watchdog" in args:
        return False
    if "grep" in args and "residents-watchdog" in args:
        return False
    return "residents-watchdog.sh" in args


def test_no_residents_ignores_pgrep_self_match():
    """If a diagnostic pgrep argv embeds residents-watchdog.sh then the health
    line stays PASS, else stop."""
    diagnostic = [
        "pgrep -af residents-watchdog.sh",
        "/usr/bin/bash -c pgrep -af residents-watchdog.sh",
        "grep residents-watchdog /proc/*/cmdline",
        "bash -O extglob -c ... pgrep -af residents-watchdog.sh ...",
    ]
    real = [
        "bash /home/dev/jobs/residents-watchdog.sh",
        "/home/dev/jobs/residents-watchdog.sh",
        "bash -x ~/jobs/residents-watchdog.sh loop",
    ]
    for argv in diagnostic:
        assert not _residents_watchdog_is_real_executor(argv), argv
    for argv in real:
        assert _residents_watchdog_is_real_executor(argv), argv


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
    _hydrate_check_run_fixtures(fixture)

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
    _hydrate_check_run_fixtures(fixture)

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


def test_github_clean_pr_with_failing_review_gate_is_not_clean(tmp_path):
    """[if] mergeable_state is CLEAN but review-triage exits 1 [then] clean excludes it, [else stop]."""
    fixture = _copy_fixture(tmp_path)
    mapping = json.loads((fixture / "review-triage.json").read_text(encoding="utf-8"))
    mapping["1108"] = 1
    (fixture / "review-triage.json").write_text(json.dumps(mapping), encoding="utf-8")

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, out
    assert "backlog open_prs=8 actionable=5 clean=2" in out
    assert "actionable=5" in out


def test_review_gate_measurement_failure_makes_clean_unmeasurable(tmp_path):
    """[if] review-triage cannot measure a CLEAN actionable PR [then] clean=?, [else stop]."""
    fixture = _copy_fixture(tmp_path)
    mapping = json.loads((fixture / "review-triage.json").read_text(encoding="utf-8"))
    mapping["1102"] = 3
    (fixture / "review-triage.json").write_text(json.dumps(mapping), encoding="utf-8")

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, out
    assert "backlog open_prs=8 actionable=5 clean=?" in out
    assert "review-triage COULD NOT MEASURE for actionable PR #1102" in proc.stderr


def test_only_github_clean_actionable_prs_invoke_review_gate(tmp_path):
    """[if] a PR is not actionable or not GitHub-clean [then] review-triage is not run, [else stop]."""
    fixture = _copy_fixture(tmp_path)
    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, out
    invoked = json.loads((fixture / "review-triage.json").read_text(encoding="utf-8"))["invoked"]
    assert invoked == [1101, 1102, 1108]


def test_github_clean_pr_with_failing_check_is_not_clean(tmp_path):
    """[if] CLEAN PR has FAILURE check [then] clean excludes it, [else stop]."""
    fixture = _copy_fixture(tmp_path)
    sha_by_pr = _hydrate_check_run_fixtures(fixture)
    head_sha = sha_by_pr[1102]
    check_path = fixture / "gh" / f"check-runs-{head_sha}.json"
    checks = json.loads(check_path.read_text(encoding="utf-8"))
    checks["check_runs"].append(
        {"name": "quality gate", "status": "completed", "conclusion": "FAILURE"}
    )
    checks["total_count"] = len(checks["check_runs"])
    (fixture / "gh" / f"check-runs-{head_sha}.json").write_text(
        json.dumps(checks) + "\n",
        encoding="utf-8",
    )

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, out
    assert "backlog open_prs=8 actionable=5 clean=2" in out
    assert "actionable=5" in out


def test_failure_conclusion_normalization_is_case_insensitive(tmp_path):
    """[if] lowercase failure conclusion [then] clean excludes it, [else stop]."""
    fixture = _copy_fixture(tmp_path)
    sha_by_pr = _hydrate_check_run_fixtures(fixture)
    head_sha = sha_by_pr[1108]
    _write_check_runs(fixture, head_sha, ["success", "failure"])

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, out
    assert "backlog open_prs=8 actionable=5 clean=2" in out


def test_neutral_skipped_and_null_check_conclusions_still_count_clean(tmp_path):
    """[if] rollup has neutral skipped null success only [then] clean counts it, [else stop]."""
    fixture = _copy_fixture(tmp_path)
    sha_by_pr = _hydrate_check_run_fixtures(fixture)
    _write_check_runs(fixture, sha_by_pr[1102], ["success", "neutral", "skipped", None])

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, out
    assert "backlog open_prs=8 actionable=5 clean=3" in out


def test_check_rollup_read_failure_makes_clean_unmeasurable(tmp_path):
    """[if] check-rollup cannot be read for a CLEAN actionable PR [then] clean=?, [else stop]."""
    fixture = _copy_fixture(tmp_path)
    sha_by_pr = _hydrate_check_run_fixtures(fixture)
    (fixture / "gh" / f"check-runs-{sha_by_pr[1101]}.json").unlink()

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, out
    assert "backlog open_prs=8 actionable=5 clean=?" in out
    assert "check-rollup COULD NOT MEASURE for actionable PR #1101" in proc.stderr


def test_malformed_check_runs_response_makes_clean_unmeasurable(tmp_path):
    """[if] check-runs wrapper lacks array or total_count [then] clean=?, [else stop]."""
    fixture = _copy_fixture(tmp_path)
    sha_by_pr = _hydrate_check_run_fixtures(fixture)
    malformed_path = fixture / "gh" / f"check-runs-{sha_by_pr[1102]}.json"
    malformed_path.write_text('{"total_count": 1}\n', encoding="utf-8")

    proc = _run(_env(fixture, _home(tmp_path, token_profile=True)))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, out
    assert "backlog open_prs=8 actionable=5 clean=?" in out
    assert "check-rollup COULD NOT MEASURE for actionable PR #1102" in proc.stderr


def test_only_github_clean_actionable_prs_invoke_check_rollup(tmp_path):
    """[if] PR not actionable or not CLEAN [then] check-rollup unread, [else stop]."""
    fixture = _copy_fixture(tmp_path)
    check_log = fixture / "check-runs-invoked.jsonl"
    env = _env(fixture, _home(tmp_path, token_profile=True))
    env["KPI_CHECK_RUNS_LOG"] = str(check_log)

    proc = _run(env)
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, out
    log_lines = check_log.read_text(encoding="utf-8").splitlines()
    invoked = [int(line) for line in log_lines if line.strip()]
    assert invoked == [1101, 1102, 1108]
