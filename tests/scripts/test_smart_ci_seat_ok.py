"""ExecCondition helper for the smart-ci lane (OPS-48, Sentry FLEET-OPS-2H).

smart-ci.service had no seat-ok ExecCondition. A walled Claude seat ran the
launcher and systemd recorded Result=exit-code ExecMainCode=1.

[if] a smart-ci seat is walled with no rotatable successor [then] seat-ok exits 1, [else stop].
"""

from __future__ import annotations

import os
import platform
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SEAT_OK = REPO / "ops" / "fleet" / "kpi-fleet" / "seat-ok.sh"
DROP_IN = REPO / "ops" / "fleet" / "units" / "smart-ci.service.d" / "seat-ok.conf"
NOW = int(datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC).timestamp())

pytestmark = [
    pytest.mark.requirement("OPS-48"),
    pytest.mark.skipif(platform.system() != "Linux", reason="GNU date -d"),
]


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jobs(tmp_path: Path) -> Path:
    jobs = tmp_path / "jobs"
    (jobs / "state").mkdir(parents=True)
    return jobs


def _write_meter(jobs: Path, acct: str, kind: str, pct: int, age_s: int) -> None:
    (jobs / "state" / f"{acct}-{kind}-pct").write_text(f"{pct}\n", encoding="utf-8")
    (jobs / "state" / f"{acct}-{kind}-measured-at").write_text(
        _iso(NOW - age_s) + "\n", encoding="utf-8"
    )


def _run(jobs: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["KPI_JOBS_DIR"] = str(jobs)
    env["KPI_NOW_UNIX"] = str(NOW)
    return subprocess.run(
        ["bash", str(SEAT_OK), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_green_seat_starts_the_unit(tmp_path: Path) -> None:
    """[if] the lane meter is under deny [then] seat-ok exits 0 with SEAT-OK, [else stop]."""
    jobs = _jobs(tmp_path)
    (jobs / "state" / "lane-accounts").write_text("smart-ci acct-green\n", encoding="utf-8")
    (jobs / "state" / "account-seats").write_text("acct-green /tmp\n", encoding="utf-8")
    _write_meter(jobs, "acct-green", "five-hour", 10, 60)
    _write_meter(jobs, "acct-green", "seven-day", 20, 60)
    proc = _run(jobs, "smart-ci.service")
    assert proc.returncode == 0, proc.stderr
    assert "SEAT-OK" in proc.stderr
    assert "lane=smart-ci" in proc.stderr
    assert "SEAT-WALLED" not in proc.stderr


def test_walled_without_successor_skips(tmp_path: Path) -> None:
    """[if] weekly wall and no successor [then] seat-ok exits 1, never 255, [else stop]."""
    jobs = _jobs(tmp_path)
    (jobs / "state" / "lane-accounts").write_text("smart-ci acct-hot\n", encoding="utf-8")
    (jobs / "state" / "account-seats").write_text("acct-hot /tmp\n", encoding="utf-8")
    _write_meter(jobs, "acct-hot", "seven-day", 100, 60)
    proc = _run(jobs, "smart-ci")
    assert proc.returncode == 1, proc.stderr
    assert proc.returncode != 255
    assert "SEAT-WALLED" in proc.stderr


def test_unit_suffix_strips_to_lane(tmp_path: Path) -> None:
    """[if] argv is smart-ci.service [then] lane-accounts key is smart-ci, [else stop]."""
    jobs = _jobs(tmp_path)
    (jobs / "state" / "lane-accounts").write_text("smart-ci acct-hot\n", encoding="utf-8")
    (jobs / "state" / "account-seats").write_text("acct-hot /tmp\n", encoding="utf-8")
    _write_meter(jobs, "acct-hot", "five-hour", 91, 60)
    proc = _run(jobs, "smart-ci.service")
    assert proc.returncode == 1, proc.stderr
    assert "lane=smart-ci" in proc.stderr


def test_walled_with_fresh_successor_starts(tmp_path: Path) -> None:
    """[if] the seat is walled but a successor meter is fresh [then] exit 0, [else stop]."""
    jobs = _jobs(tmp_path)
    (jobs / "state" / "lane-accounts").write_text("smart-ci acct-hot\n", encoding="utf-8")
    (jobs / "state" / "account-seats").write_text(
        "acct-hot /tmp\nacct-cold /tmp\n", encoding="utf-8"
    )
    (jobs / "state" / "account-rotation").write_text(
        "smart-ci acct-hot acct-cold\n", encoding="utf-8"
    )
    _write_meter(jobs, "acct-hot", "seven-day", 100, 60)
    _write_meter(jobs, "acct-cold", "five-hour", 10, 60)
    proc = _run(jobs, "smart-ci")
    assert proc.returncode == 0, proc.stderr
    assert "rotate-to=acct-cold" in proc.stderr


def test_missing_lane_map_is_observe_only(tmp_path: Path) -> None:
    """[if] lane-accounts has no row [then] exit 0 UNMEASURABLE, not a fail, [else stop]."""
    jobs = _jobs(tmp_path)
    proc = _run(jobs, "smart-ci.service")
    assert proc.returncode == 0, proc.stderr
    assert "UNMEASURABLE" in proc.stderr


def test_smart_ci_drop_in_calls_seat_ok() -> None:
    """[if] the smart-ci drop-in is read [then] ExecCondition calls seat-ok.sh, [else stop]."""
    text = DROP_IN.read_text(encoding="utf-8")
    assert "ExecCondition=/bin/bash %h/jobs/kpi-fleet/seat-ok.sh %p" in text
    assert "FLEET-OPS-2H" in text
    mode = SEAT_OK.stat().st_mode
    assert mode & stat.S_IXUSR
