"""e2e.yml runs the gate on main's tip every hour, and the nightly tier on its own cron only.

ADR-NEW-e2e-opt-in-by-label (Mon 9 Nov 2026, issue #10534): a pull request's E2E is opt-in
by the `e2e` label, so the E2E that completes is this hourly schedule run. Main's push runs
completed 0 of 59 in the Mon 9 Nov sample (ship-mode trunk-tip-only cancels queued main runs).

Single-line intent:
  - [if] the hourly cron is dropped or lands on the hour [then] main has no completing E2E again, [else stop].
  - [if] the extended tier runs for the hourly cron [then] 24 nightly suites a day burn the e2e seats, [else stop].
  - [if] the extended tier stops running for the 04:17 cron [then] the nightly suite never runs, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.requirement("DEVOPS-10534")

E2E = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "e2e.yml"
NIGHTLY_CRON = "17 4 * * *"
HOURLY_CRON = "47 * * * *"


def _workflow() -> dict:
    return yaml.safe_load(E2E.read_text(encoding="utf-8"))


def test_the_schedule_carries_the_nightly_and_the_hourly_cron() -> None:
    """[if] the hourly cron is dropped or lands on the hour [then] main has no completing E2E again, [else stop]."""
    crons = [entry["cron"] for entry in _workflow()[True]["schedule"]]
    assert crons == [NIGHTLY_CRON, HOURLY_CRON]
    minute, hour = HOURLY_CRON.split()[:2]
    assert hour == "*" and minute not in {"0", "23", "3", "13", "33", "43", "53", "11"}


def test_the_extended_tier_runs_for_the_nightly_cron_only() -> None:
    """[if] the extended tier runs for the hourly cron [then] 24 nightly suites a day burn the e2e seats, [else stop]."""
    condition = " ".join(str(_workflow()["jobs"]["extended"]["if"]).split())
    assert f"github.event_name == 'schedule' && github.event.schedule == '{NIGHTLY_CRON}'" in condition
    assert "(github.event_name == 'workflow_dispatch' && inputs.extended)" in condition
    assert "!cancelled()" in condition
    assert HOURLY_CRON not in condition
