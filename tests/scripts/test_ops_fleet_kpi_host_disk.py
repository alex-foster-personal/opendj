"""The host-disk health line: kpi.sh reads the Windows host's view of C:, not guest df.

The WSL disk lives on the host's C: drive. On Thu 10 Sep 2026 guest df read ~480 GB free
while C: sat at 0 GB and every guest read returned EIO (issue #1813), so no guest-side probe
can see this failure. af-disk-watchdog, a Windows scheduled task, writes
state/host-disk.json every 5 min and kpi.sh turns it into one health line.

[if] the host-disk line passes on anything but a fresh GREEN reading [then] fail, [else stop].

Regression lines:
  - [if] a fresh GREEN reading does not PASS [then] fail, [else stop].
  - [if] a fresh non-GREEN level passes [then] fail, [else stop].
  - [if] a reading older than 15 min passes [then] fail, [else stop]. A watchdog that
    stopped leaves its last GREEN behind, which is the false green this line exists to stop.
  - [if] a missing or unparseable file renders PASS or FAIL [then] fail, [else stop].
  - [if] the watchdog's fractional-second timestamp is unreadable [then] fail, [else stop].
"""

from __future__ import annotations

import json
import platform
import shutil

import pytest

from tests.scripts.test_ops_fleet_kpi import NOW, _copy_fixture, _env, _health, _home, _iso, _run

pytestmark = [
    pytest.mark.requirement("OPS-16"),
    pytest.mark.skipif(
        platform.system() != "Linux",
        reason="ops/fleet/kpi.sh is Linux-only: GNU date -d/stat -c, /proc, systemd --user, tmux",
    ),
    pytest.mark.skipif(shutil.which("jq") is None, reason="ops/fleet/kpi.sh parses JSON with jq"),
]

LABEL_PREFIX = "host disk GREEN ("


def _host_disk_verdict(tmp_path, reading: str | None) -> tuple[str, str]:
    fixture = _copy_fixture(tmp_path)
    state = fixture / "jobs" / "state" / "host-disk.json"
    state.unlink(missing_ok=True)
    if reading is not None:
        state.write_text(reading)
    out = _run(_env(fixture, _home(tmp_path, token_profile=True))).stdout
    matches = [
        (label, verdict)
        for label, verdict in _health(out).items()
        if label.startswith(LABEL_PREFIX)
    ]
    assert len(matches) == 1, out
    label, verdict = matches[0]
    return verdict, label


def _reading(level: str, age_s: int, ts: str | None = None) -> str:
    return json.dumps(
        {
            "level": level,
            "free_gb": 160.17,
            "rate_gb_per_h": 3.8,
            "hours_to_red": 34,
            "vhdx_gb": 610.3,
            "ts": ts or _iso(NOW - age_s),
        }
    )


def test_a_fresh_green_reading_passes(tmp_path):
    """[if] a GREEN reading written 2 min ago does not PASS [then] fail, [else stop]."""
    verdict, label = _host_disk_verdict(tmp_path, _reading("GREEN", 120))
    assert verdict == "PASS", label
    assert "free=160.17GB" in label, label


@pytest.mark.parametrize("level", ["YELLOW", "ORANGE", "RED", "CRITICAL"])
def test_a_fresh_non_green_level_fails(tmp_path, level):
    """[if] a fresh reading at any level but GREEN passes [then] fail, [else stop]."""
    verdict, label = _host_disk_verdict(tmp_path, _reading(level, 120))
    assert verdict == "FAIL", label


def test_a_stale_green_reading_fails(tmp_path):
    """[if] a GREEN reading 16 min old passes [then] fail, [else stop]."""
    verdict, label = _host_disk_verdict(tmp_path, _reading("GREEN", 960))
    assert verdict == "FAIL", label


def test_a_green_reading_just_inside_the_window_passes(tmp_path):
    """[if] a GREEN reading 14 min old fails [then] fail, [else stop]. Control for staleness."""
    verdict, label = _host_disk_verdict(tmp_path, _reading("GREEN", 840))
    assert verdict == "PASS", label


def test_a_future_green_reading_fails(tmp_path):
    """[if] a GREEN reading stamped 20 min in the future passes [then] fail, [else stop]."""
    verdict, label = _host_disk_verdict(tmp_path, _reading("GREEN", -1200))
    assert verdict == "FAIL", label


def test_a_green_reading_within_clock_skew_passes(tmp_path):
    """[if] a GREEN reading 30 s ahead fails [then] fail, [else stop]. Control for future ts."""
    verdict, label = _host_disk_verdict(tmp_path, _reading("GREEN", -30))
    assert verdict == "PASS", label


def test_the_watchdog_fractional_timestamp_parses(tmp_path):
    """[if] PowerShell's 7-digit fractional ts does not read as fresh [then] fail, [else stop]."""
    ts = _iso(NOW - 60).replace("Z", ".7173594Z")
    verdict, label = _host_disk_verdict(tmp_path, _reading("GREEN", 60, ts=ts))
    assert verdict == "PASS", label


@pytest.mark.parametrize(
    "reading", [None, "not json", "{}"], ids=["missing", "garbage", "empty-object"]
)
def test_an_unreadable_reading_is_unmeasurable(tmp_path, reading):
    """[if] a missing or unparseable host-disk.json gives PASS or FAIL [then] fail, [else stop]."""
    verdict, label = _host_disk_verdict(tmp_path, reading)
    assert verdict == "UNMEASURABLE", label
