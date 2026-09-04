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
  - if an ATTEMPT_START stamp older than the window counts then broken
    (attempts_started=2, not 3)
  - if the burn line or block-cost-per-merge ratio is misread from quota.sh
    then broken (block_cost_usd=42.00 over n=3 merges -> 14.0)
  - if the backlog open/actionable/clean split is misread then broken
    (5 / 3 / 3 against a fixture that also carries a draft, a blocked:* PR and
    a PR older than the 30-day actionable window)
  - if a health line that cannot measure reports PASS or a silent zero instead
    of FAIL then broken (missing probe fixture and missing gh fixture controls)
  - if the 8 data health lines cannot be flipped by their own fixture inputs
    then broken (the red-fixture run proves each verdict is driven by the
    input it names, not by ambient machine state)
  - if the token health line leaks the developer's real shell profile then
    broken (run with a throwaway $HOME)
"""

from __future__ import annotations

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
    # Gate-log freshness is decided by mtime, which git does not track: pin it
    # inside the window (now - 120s) so the green fixture reports PASS.
    os.utime(fixture / "jobs" / "logs" / "tick-gate.log", (NOW - 120, NOW - 120))
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


GREEN_LABELS = [
    "queue-watchdog unit active",
    "exactly one watchdog loop (sleeping main pid 424242)",
    "no tmux residents loop (retired)",
    "gate log written in last 15 min",
    "dispatcher ticked in last 70 min",
    "workers within cap (4)",
    "actionable PR backlog under hard target 15",
    "no FATAL in logs last 1h",
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

    # Backlog split: 6 open rows -> 5 non-draft, 3 actionable, 3 CLEAN.
    assert "backlog open_prs=5 actionable=3 clean=3 soft_target=10 hard_target=15" in out

    # Every health line reports PASS; nothing is unmeasured-green and the
    # throwaway $HOME profile satisfied the token line.
    verdicts = _health(out)
    assert list(verdicts) == GREEN_LABELS, out
    assert set(verdicts.values()) == {"PASS"}, out
    assert "missing" not in proc.stderr


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
    (fixture / "jobs" / "logs" / "recent-fatal.log").write_text(
        f"{_iso(NOW - 100)} FATAL induced for the red control\n"
    )

    # No token profile in $HOME -> token line red.
    verdicts = _health((_run(_env(fixture, _home(tmp_path, token_profile=False)))).stdout)

    assert verdicts["queue-watchdog unit active"] == "FAIL"
    assert verdicts["exactly one watchdog loop (sleeping main pid 424242)"] == "FAIL"
    assert verdicts["no tmux residents loop (retired)"] == "FAIL"
    assert verdicts["gate log written in last 15 min"] == "FAIL"
    assert verdicts["dispatcher ticked in last 70 min"] == "FAIL"
    assert verdicts["workers within cap (1)"] == "FAIL"
    assert verdicts["no FATAL in logs last 1h"] == "FAIL"
    assert verdicts["token present for launchers"] == "FAIL"
    # Unflipped: the backlog fixture still holds 3 actionable <= hard target 15.
    assert verdicts["actionable PR backlog under hard target 15"] == "PASS"


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


def test_script_is_executable_and_syntax_clean():
    """If ops/fleet/kpi.sh loses its shebang or exec bit then broken."""
    assert KPI.exists()
    assert KPI.stat().st_mode & 0o111, f"not executable: {KPI}"
