"""Hermetic tests for ops/fleet/kpi-harness.sh synthetic eval harness.

The harness builds discardable fixture copies under mktemp, drives kpi.sh
through its KPI_* seams, and measures weekly allowance cost from the account
meter. Never touches live ~/jobs.

Regression lines:
  - [if] the harness script is absent or not executable [then] fail, [else stop]
  - [if] the green pack cannot see merges n=3, skip ratios, and health PASS [then] fail, [else stop]
  - [if] retired-alert pack omits RETIRED or ALERT [then] fail, [else stop]
  - [if] skip-review pack prints 0 or absence instead of skipped [then] fail, [else stop]
  - [if] unmeasurable-health pack prints PASS or absence instead of FAIL [then] fail, [else stop]
  - [if] cost delta exceeds cap [then] fail, [else stop]
  - [if] meter is missing [then] delta_pct=unmeasurable verdict=FAIL, never 0, [else stop]
  - [if] account is unset [then] verdict=FAIL, never a guessed zero, [else stop]
  - [if] harness writes to live ~/jobs [then] fail, [else stop]
  - [if] harness calls network gh [then] fail, [else stop]
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.scripts.test_ops_fleet_kpi import (
    FIXTURE,
    NOW,
    REPO,
)

HARNESS = REPO / "ops" / "fleet" / "kpi-harness.sh"
ACCOUNT = "acct-green"

pytestmark = [
    pytest.mark.requirement("OPS-16"),
    pytest.mark.skipif(
        platform.system() != "Linux",
        reason="ops/fleet/kpi-harness.sh is Linux-only: GNU date -d/stat -c",
    ),
    pytest.mark.skipif(
        shutil.which("jq") is None,
        reason="ops/fleet/kpi.sh parses the gh JSON fixtures with jq",
    ),
]


def _meter_setup(
    tmp_path: Path,
    before_pct: str,
    after_pct: str | None = None,
) -> tuple[Path, Path | None]:
    """Write temp meter files. Returns (before_dir, after_dir_or_None)."""
    before_dir = tmp_path / "meter-before"
    before_dir.mkdir()
    (before_dir / f"{ACCOUNT}-seven-day-pct").write_text(before_pct)
    (before_dir / f"{ACCOUNT}-seven-day-measured-at").write_text("2026-09-11T12:00:00Z")
    if after_pct is None:
        return before_dir, None
    after_dir = tmp_path / "meter-after"
    after_dir.mkdir()
    (after_dir / f"{ACCOUNT}-seven-day-pct").write_text(after_pct)
    (after_dir / f"{ACCOUNT}-seven-day-measured-at").write_text("2026-09-11T12:00:01Z")
    return before_dir, after_dir


def _harness_env(
    tmp_path: Path,
    meter_dir: Path,
    after_dir: Path | None = None,
    account: str = ACCOUNT,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    profile = home / ".profile"
    if not profile.exists():
        profile.write_text("export CLAUDE_CODE_OAUTH_TOKEN=fixture-token\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("KPI_")}
    env.update(
        {
            "HOME": str(home),
            "KPI_HARNESS_ACCOUNT": account,
            "KPI_METER_DIR": str(meter_dir),
            "KPI_NOW_UNIX": str(NOW),
            "KPI_HARNESS_FIXTURE_DIR": str(FIXTURE),
        }
    )
    if after_dir is not None:
        env["KPI_METER_AFTER_DIR"] = str(after_dir)
    if extra:
        env.update(extra)
    return env


def _run_harness(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )


def _cost_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("harness_cost "):
            return line
    raise AssertionError(f"no harness_cost line in output:\n{out}")


def test_harness_script_is_executable_and_syntax_clean():
    """[if] the harness script is absent or not executable [then] fail, [else stop]."""
    assert HARNESS.is_file(), f"missing {HARNESS}"
    assert os.access(HARNESS, os.X_OK), f"not executable: {HARNESS}"
    proc = subprocess.run(
        ["bash", "-n", str(HARNESS)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_green_pack_passes_against_the_committed_fixture(tmp_path):
    """[if] the green pack cannot see merges n=3, skip ratios, and health PASS
    [then] fail, [else stop]."""
    meter_dir, _ = _meter_setup(tmp_path, "10.0", "10.0")
    proc = _run_harness(_harness_env(tmp_path, meter_dir, after_dir=tmp_path / "meter-after"))
    out = proc.stdout + proc.stderr
    assert "harness case=green verdict=PASS" in out, out
    assert "merges n=3" in out, out
    assert "skip_ratio=" in out, out

    # Negative: stub kpi.sh that omits skip_ratio must FAIL the green pack.
    stub = tmp_path / "stub-no-skip.sh"
    stub.write_text(
        textwrap.dedent("""\
        #!/bin/bash
        echo "merges n=3 per_hour=3.00"
        echo "health PASS queue-watchdog unit active"
        """)
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = _harness_env(
        tmp_path,
        meter_dir,
        after_dir=tmp_path / "meter-after",
        extra={"KPI_SH": str(stub)},
    )
    proc = _run_harness(env)
    assert "harness case=green verdict=FAIL" in proc.stdout, proc.stdout


def test_retired_and_alert_pack_requires_both_lines(tmp_path):
    """[if] retired-alert pack omits RETIRED or ALERT [then] fail, [else stop]."""
    meter_dir, after_dir = _meter_setup(tmp_path, "10.0", "10.0")
    proc = _run_harness(_harness_env(tmp_path, meter_dir, after_dir=after_dir))
    out = proc.stdout + proc.stderr
    assert "harness case=retired-alert verdict=PASS" in out, out
    assert "ticks lane=merge-even RETIRED" in out, out
    assert "skip_ratio=ALERT" in out, out

    # Negative: stub kpi.sh that omits RETIRED and ALERT must FAIL the pack.
    stub = tmp_path / "stub-kpi.sh"
    stub.write_text(
        textwrap.dedent("""\
        #!/bin/bash
        echo "ticks lane=merge-even ran=1 skipped=1 skip_ratio=0.50"
        echo "ticks lane=merge-odd ran=1 skipped=1 skip_ratio=0.50"
        echo "merges n=3 per_hour=3.00"
        echo "health PASS queue-watchdog unit active"
        """)
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = _harness_env(tmp_path, meter_dir, after_dir=after_dir, extra={"KPI_SH": str(stub)})
    proc = _run_harness(env)
    assert "harness case=retired-alert verdict=FAIL" in proc.stdout, proc.stdout


def test_skip_review_pack_names_the_skip_and_does_not_read_reviews(tmp_path):
    """[if] skip-review pack prints 0 or absence instead of skipped [then] fail, [else stop]."""
    meter_dir, after_dir = _meter_setup(tmp_path, "10.0", "10.0")
    proc = _run_harness(_harness_env(tmp_path, meter_dir, after_dir=after_dir))
    out = proc.stdout + proc.stderr
    assert "harness case=skip-review verdict=PASS" in out, out
    assert "codex_reviews_per_merged_pr=skipped" in out, out

    # Stub printing 0 must FAIL.
    stub_zero = tmp_path / "stub-zero.sh"
    stub_zero.write_text("#!/bin/bash\necho 'codex_reviews_per_merged_pr=0'\n")
    stub_zero.chmod(stub_zero.stat().st_mode | stat.S_IEXEC)
    env = _harness_env(
        tmp_path, meter_dir, after_dir=after_dir, extra={"KPI_SH": str(stub_zero)}
    )
    proc = _run_harness(env)
    assert "harness case=skip-review verdict=FAIL" in proc.stdout, proc.stdout

    # Stub printing nothing about the ratio must FAIL (absence is not a skip).
    stub_absent = tmp_path / "stub-absent.sh"
    stub_absent.write_text("#!/bin/bash\necho 'merges n=3'\n")
    stub_absent.chmod(stub_absent.stat().st_mode | stat.S_IEXEC)
    env = _harness_env(
        tmp_path, meter_dir, after_dir=after_dir, extra={"KPI_SH": str(stub_absent)}
    )
    proc = _run_harness(env)
    assert "harness case=skip-review verdict=FAIL" in proc.stdout, proc.stdout


def test_unmeasurable_health_pack_requires_fail_not_pass(tmp_path):
    """[if] unmeasurable-health pack prints PASS or absence instead of FAIL
    [then] fail, [else stop]."""
    meter_dir, after_dir = _meter_setup(tmp_path, "10.0", "10.0")
    proc = _run_harness(_harness_env(tmp_path, meter_dir, after_dir=after_dir))
    out = proc.stdout + proc.stderr
    assert "harness case=unmeasurable-health verdict=PASS" in out, out
    assert "health FAIL queue-watchdog unit active" in out, out

    # Stub printing PASS must FAIL the pack.
    stub_pass = tmp_path / "stub-pass.sh"
    stub_pass.write_text("#!/bin/bash\necho 'health PASS queue-watchdog unit active'\n")
    stub_pass.chmod(stub_pass.stat().st_mode | stat.S_IEXEC)
    env = _harness_env(
        tmp_path, meter_dir, after_dir=after_dir, extra={"KPI_SH": str(stub_pass)}
    )
    proc = _run_harness(env)
    assert "harness case=unmeasurable-health verdict=FAIL" in proc.stdout, proc.stdout

    # Stub printing nothing about that health line must FAIL (absence is not FAIL).
    stub_absent = tmp_path / "stub-absent-health.sh"
    stub_absent.write_text("#!/bin/bash\necho 'merges n=3'\n")
    stub_absent.chmod(stub_absent.stat().st_mode | stat.S_IEXEC)
    env = _harness_env(
        tmp_path, meter_dir, after_dir=after_dir, extra={"KPI_SH": str(stub_absent)}
    )
    proc = _run_harness(env)
    assert "harness case=unmeasurable-health verdict=FAIL" in proc.stdout, proc.stdout


def test_cost_delta_at_or_under_cap_passes(tmp_path):
    """[if] cost delta at or under cap does not pass [then] fail, [else stop]."""
    meter_dir, after_dir = _meter_setup(tmp_path, "10.0", "10.4")
    proc = _run_harness(_harness_env(tmp_path, meter_dir, after_dir=after_dir))
    cost = _cost_line(proc.stdout)
    assert "delta_pct=0.4" in cost, cost
    assert "verdict=PASS" in cost, cost
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_cost_delta_over_cap_fails(tmp_path):
    """[if] cost delta over cap does not fail [then] fail, [else stop]."""
    meter_dir, after_dir = _meter_setup(tmp_path, "10.0", "10.6")
    proc = _run_harness(_harness_env(tmp_path, meter_dir, after_dir=after_dir))
    cost = _cost_line(proc.stdout)
    assert "verdict=FAIL" in cost, cost
    assert proc.returncode != 0, proc.stdout + proc.stderr


def test_missing_meter_is_fail_never_zero(tmp_path):
    """[if] missing meter prints delta_pct=0 or verdict=PASS [then] fail, [else stop]."""
    empty_meter = tmp_path / "empty-meter"
    empty_meter.mkdir()
    proc = _run_harness(_harness_env(tmp_path, empty_meter))
    cost = _cost_line(proc.stdout)
    assert "delta_pct=unmeasurable" in cost, cost
    assert "verdict=FAIL" in cost, cost
    assert "delta_pct=0" not in cost, cost
    assert "verdict=PASS" not in cost.split("harness_cost", 1)[-1], cost
    assert "seven-day-pct" in proc.stderr, proc.stderr


def test_missing_account_is_fail_never_a_guessed_zero(tmp_path):
    """[if] missing account prints delta_pct=0 [then] fail, [else stop]."""
    meter_dir, _ = _meter_setup(tmp_path, "10.0")
    env = _harness_env(tmp_path, meter_dir)
    del env["KPI_HARNESS_ACCOUNT"]
    proc = _run_harness(env)
    cost = _cost_line(proc.stdout)
    assert "account=unmeasurable" in cost, cost
    assert "verdict=FAIL" in cost, cost
    assert "delta_pct=0" not in cost, cost
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_unparseable_meter_pct_is_fail_never_zero(tmp_path):
    """[if] unparseable meter pct prints delta_pct=0 or verdict=PASS [then] fail, [else stop]."""
    meter_dir = tmp_path / "meter-garbage"
    meter_dir.mkdir()
    (meter_dir / f"{ACCOUNT}-seven-day-pct").write_text("not-a-number")
    (meter_dir / f"{ACCOUNT}-seven-day-measured-at").write_text("2026-09-11T12:00:00Z")
    proc = _run_harness(_harness_env(tmp_path, meter_dir))
    cost = _cost_line(proc.stdout)
    assert "delta_pct=unmeasurable" in cost, cost
    assert "verdict=FAIL" in cost, cost
    assert "delta_pct=0" not in cost, cost
    assert "unparseable meter pct" in proc.stderr, proc.stderr
    assert proc.returncode == 2, proc.stdout + proc.stderr

    # Negative: numeric pct must not trip the unparseable guard.
    meter_dir, after_dir = _meter_setup(tmp_path, "10.0", "10.0")
    proc = _run_harness(_harness_env(tmp_path, meter_dir, after_dir=after_dir))
    cost = _cost_line(proc.stdout)
    assert "delta_pct=0.0" in cost, cost
    assert "verdict=PASS" in cost, cost


def test_missing_meter_after_dir_is_fail_with_cost_line(tmp_path):
    """[if] KPI_METER_AFTER_DIR is missing [then] harness_cost FAILs loudly, [else stop]."""
    meter_dir, _ = _meter_setup(tmp_path, "10.0")
    env = _harness_env(tmp_path, meter_dir, after_dir=tmp_path / "missing-after-dir")
    proc = _run_harness(env)
    cost = _cost_line(proc.stdout)
    assert "after=unmeasurable" in cost, cost
    assert "delta_pct=unmeasurable" in cost, cost
    assert "verdict=FAIL" in cost, cost
    assert "KPI_METER_AFTER_DIR" in proc.stderr, proc.stderr
    assert proc.returncode == 2, proc.stdout + proc.stderr

    # Negative: present after-dir must measure a numeric delta.
    ok_root = tmp_path / "ok"
    ok_root.mkdir()
    meter_dir2, after_dir = _meter_setup(ok_root, "10.0", "10.0")
    proc = _run_harness(_harness_env(ok_root, meter_dir2, after_dir=after_dir))
    cost = _cost_line(proc.stdout)
    assert "after=10.0" in cost, cost
    assert "verdict=PASS" in cost, cost


def test_harness_does_not_write_the_live_jobs_tree(tmp_path):
    """[if] harness writes to live ~/jobs [then] fail, [else stop]."""
    home = tmp_path / "sentinel-home"
    home.mkdir()
    jobs = home / "jobs"
    jobs.mkdir()
    sentinel = jobs / "sentinel"
    sentinel.write_bytes(b"DO_NOT_TOUCH\n")
    original = sentinel.read_bytes()

    meter_dir, after_dir = _meter_setup(tmp_path, "10.0", "10.0")
    env = {k: v for k, v in os.environ.items() if not k.startswith("KPI_")}
    env.update(
        {
            "HOME": str(home),
            "KPI_HARNESS_ACCOUNT": ACCOUNT,
            "KPI_METER_DIR": str(meter_dir),
            "KPI_METER_AFTER_DIR": str(after_dir),
            "KPI_NOW_UNIX": str(NOW),
            "KPI_HARNESS_FIXTURE_DIR": str(FIXTURE),
        }
    )
    (home / ".profile").write_text("export CLAUDE_CODE_OAUTH_TOKEN=fixture-token\n")
    _run_harness(env)

    assert sentinel.read_bytes() == original, "harness wrote to $HOME/jobs"


def test_harness_does_not_call_the_network_gh(tmp_path):
    """[if] harness calls network gh [then] fail, [else stop]."""
    gh_stub_dir = tmp_path / "gh-stub-bin"
    gh_stub_dir.mkdir()
    marker = tmp_path / "gh-called-marker"
    gh_stub = gh_stub_dir / "gh"
    gh_stub.write_text(
        textwrap.dedent(f"""\
        #!/bin/bash
        echo "CALLED" > {marker}
        exit 2
        """)
    )
    gh_stub.chmod(gh_stub.stat().st_mode | stat.S_IEXEC)

    meter_dir, after_dir = _meter_setup(tmp_path, "10.0", "10.0")
    env = _harness_env(tmp_path, meter_dir, after_dir=after_dir)
    env["PATH"] = f"{gh_stub_dir}:{env.get('PATH', '')}"
    proc = _run_harness(env)

    assert not marker.exists(), "harness invoked network gh"
    assert proc.returncode == 0, proc.stdout + proc.stderr
