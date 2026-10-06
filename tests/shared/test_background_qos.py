"""macOS background QoS for the ahead drain's lane children (PERF-DRAIN-01).

Regression lines:
  - if the queue child on darwin does not start under taskpolicy -b then lanes take performance cores
  - if linux or windows argv gains taskpolicy then the child never starts there
  - if a missing taskpolicy on darwin falls back to nice-only then the starvation returns silently

[if] the drain lane child skips macOS background QoS [then] fail, [else stop].
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import pytest

from apps.shared import process_priority as pp
from apps.webui.server import ahead_analysis as aa

pytestmark = pytest.mark.requirement("PERF-DRAIN-01")

ARGV = ["python", "-m", "apps.analysis.queue_cli", "--json", "status"]


def test_the_launcher_is_the_base_system_taskpolicy() -> None:
    """[if] the launcher path is not /usr/sbin/taskpolicy [then] fail, [else stop]."""
    assert pp.TASKPOLICY == "/usr/sbin/taskpolicy"


def test_darwin_argv_starts_with_taskpolicy_b() -> None:
    """[if] the platform is darwin [then] argv starts with taskpolicy -b, [else stop]."""
    out = pp.background_argv(ARGV, platform="darwin", taskpolicy=sys.executable)
    assert out == [sys.executable, "-b", *ARGV]


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_other_platforms_keep_the_argv_unchanged(platform: str) -> None:
    """[if] the platform is not darwin [then] argv is unchanged, [else stop]."""
    assert pp.background_argv(ARGV, platform=platform, taskpolicy=sys.executable) == ARGV


def test_missing_taskpolicy_on_darwin_fails_fast(tmp_path: Any) -> None:
    """[if] taskpolicy is missing on darwin [then] FileNotFoundError names it, [else stop]."""
    missing = str(tmp_path / "taskpolicy")
    with pytest.raises(FileNotFoundError, match="taskpolicy is missing"):
        pp.background_argv(ARGV, platform="darwin", taskpolicy=missing)


@pytest.mark.parametrize(("platform", "wrapped"), [("darwin", True), ("linux", False)])
def test_the_drain_queue_child_goes_through_the_qos_seam(
    monkeypatch: pytest.MonkeyPatch, platform: str, wrapped: bool
) -> None:
    """[if] _queue_cli skips the QoS seam on any platform [then] fail, [else stop]."""
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pp.sys, "platform", platform)
    monkeypatch.setattr(pp, "TASKPOLICY", sys.executable)
    monkeypatch.setattr(aa.subprocess, "run", fake_run)
    assert aa._queue_cli(["status"], os.devnull)[0] == 0
    assert (seen["argv"][:2] == [sys.executable, "-b"]) is wrapped
    assert seen["argv"][-4:] == ["--db", os.devnull, "--json", "status"]
