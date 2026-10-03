"""SETTLE_S lines from mode_ratio_browser.mjs stamp steady-state phase dicts."""

from __future__ import annotations

import subprocess
import sys

import pytest

from scripts.perf import capture_mode_ratios as cmr

_PROTOCOL_CHILD = """
import sys
for line in sys.argv[1:]:
    print(line, flush=True)
sys.stdin.read()
"""


def _spawn_protocol_lines(lines: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _PROTOCOL_CHILD, *lines],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _terminate_child(child: subprocess.Popen[str]) -> None:
    if child.stdin is not None:
        child.stdin.close()
    child.terminate()
    child.wait(timeout=5)


@pytest.mark.timeout(20)
@pytest.mark.requirement("PERFMODE-15")
def test_settle_s_sixty_stamps_phase_dict() -> None:
    """[if] helper prints SETTLE_S 60 before ready [then] phase gets settle_s=60, [else stop]."""
    child = _spawn_protocol_lines(["SETTLE_S 60", "GIG_READY"])
    try:
        settle_s = cmr._read_phase_ready(child, "GIG_READY")
        phase: dict[str, float] = {"footprint_mb": 100.0}
        cmr._attach_settle_s(phase, settle_s)
        assert settle_s == 60.0
        assert phase["settle_s"] == 60.0
    finally:
        _terminate_child(child)


@pytest.mark.timeout(20)
@pytest.mark.requirement("PERFMODE-15")
def test_absent_settle_s_leaves_phase_dict_unset() -> None:
    """[if] helper omits SETTLE_S [then] phase dict has no settle_s key, [else stop]."""
    child = _spawn_protocol_lines(["TRACKIFY_READY"])
    try:
        settle_s = cmr._read_phase_ready(child, "TRACKIFY_READY")
        phase: dict[str, float] = {"footprint_mb": 50.0}
        cmr._attach_settle_s(phase, settle_s)
        assert settle_s is None
        assert "settle_s" not in phase
    finally:
        _terminate_child(child)
