"""Regression tests for the --collect-floor guard in scripts/pytest_reqs_plugin.py.

The floor is the control that makes `pytest -n` safe to run as a gate:
parallel execution changes how tests are distributed, never which ones exist,
so a run that collects fewer tests than the floor has genuinely lost some and
must fail loudly instead of reporting a smaller green suite.

- if a run under the floor exits zero then broken
- if a run under the floor does not NAME the shortfall then broken (an xdist
  worker's exception is swallowed into a bare INTERNALERROR traceback, so the
  message has to reach stderr on its own)
- if the floor is absent or zero then a scoped run must be unaffected
- if -k or -m is accepted alongside a floor then broken: a deselected run that
  satisfied the floor would be the exact false green the floor exists to catch
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Two tests, collected without running anything: enough to prove the shortfall
# path without paying for a real suite.
SCOPE = "tests/webui/test_writeback_cli.py"


def _collect(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "--collect-only", "-q", "--no-coverage-matrix",
            "-p", "no:cacheprovider",
            SCOPE, *extra,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_run_under_the_floor_fails_and_says_why() -> None:
    result = _collect("--collect-floor", "99999")

    assert result.returncode != 0, (
        "a suite that collected 2 of a required 99999 tests exited zero; the "
        "floor is not gating anything"
    )
    combined = result.stdout + result.stderr
    assert "collect floor" in combined, combined[-2000:]
    assert "99999" in combined, "the failure must name the floor it missed"


def test_a_selection_filter_cannot_ride_along_with_a_floor() -> None:
    """-k / -m plus a floor is the false green the floor exists to prevent."""
    for flag, value in (("-k", "apply"), ("-m", "not slow")):
        result = _collect("--collect-floor", "1", flag, value)
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            f"{flag} was accepted alongside --collect-floor; a deselected run "
            "can now satisfy the floor"
        )
        assert flag in combined and "collect floor" in combined, combined[-2000:]


def test_no_floor_leaves_a_scoped_run_alone() -> None:
    """Tier 0 runs one file on purpose; a floor firing there teaches nothing."""
    assert _collect().returncode == 0
    assert _collect("--collect-floor", "0").returncode == 0
    assert _collect("--collect-floor", "1").returncode == 0
