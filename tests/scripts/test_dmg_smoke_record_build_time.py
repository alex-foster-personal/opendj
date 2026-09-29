"""Direct contract tests for ops/dmg-smoke/record_build_time.sh (DEVOPS-04, PR #4481).

This is the ONE executable path that writes a `phase=build_total` verdict to
ops/logs/ship-dmg.log: the justfile `dmg` recipe's EXIT trap delegates to it,
and the dmg-smoke test harness's headless build shim
(tests/scripts/dmg_smoke_harness.py) invokes this SAME file, so the two
cannot drift apart the way a hand-duplicated or regex-extracted copy of its
format string could (review round 2, P1: "factor the writer into an
executable production path and exercise that instead").

[if] record_build_time.sh runs against a real budget file [then] it appends
the exact verdict its elapsed/rc inputs predict, [else stop].
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.scripts.dmg_smoke_harness import REAL_BUILD_BUDGET, RECORD_BUILD_TIME

pytestmark = pytest.mark.requirement("DEVOPS-04")


def _run(root: Path, elapsed: int, rc: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(RECORD_BUILD_TIME), str(root), str(elapsed), str(rc)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _log_line(root: Path) -> str:
    return (root / "ops" / "logs" / "ship-dmg.log").read_text(encoding="utf-8").strip()


def _with_real_budget(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "ops").mkdir(parents=True)
    (root / "ops" / "build-budget.env").write_text(
        REAL_BUILD_BUDGET.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return root


def test_under_soft_budget_reads_ok(tmp_path: Path) -> None:
    root = _with_real_budget(tmp_path)
    proc = _run(root, 60, 0)
    assert proc.returncode == 0
    assert "phase=build_total seconds=60 rc=0 verdict=OK" in _log_line(root)


def test_over_soft_under_hard_reads_flag_soft(tmp_path: Path) -> None:
    root = _with_real_budget(tmp_path)
    # Real BUILD_TOTAL_SOFT_S=1320, BUILD_TOTAL_HARD_S=2700.
    proc = _run(root, 1400, 0)
    assert proc.returncode == 0
    assert "seconds=1400 rc=0 verdict=FLAG_SOFT" in _log_line(root)


def test_over_hard_budget_reads_flag_hard(tmp_path: Path) -> None:
    root = _with_real_budget(tmp_path)
    proc = _run(root, 3000, 0)
    assert proc.returncode == 0
    assert "seconds=3000 rc=0 verdict=FLAG_HARD" in _log_line(root)


def test_nonzero_rc_appends_failed_suffix_exactly_once(tmp_path: Path) -> None:
    root = _with_real_budget(tmp_path)
    proc = _run(root, 60, 1)
    assert proc.returncode == 0
    line = _log_line(root)
    assert "verdict=OK_FAILED" in line
    assert "OK_FAILED_FAILED" not in line


def test_appends_rather_than_overwrites(tmp_path: Path) -> None:
    root = _with_real_budget(tmp_path)
    _run(root, 60, 0)
    _run(root, 70, 0)
    lines = (root / "ops" / "logs" / "ship-dmg.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "seconds=60" in lines[0]
    assert "seconds=70" in lines[1]


def test_unreadable_budget_and_zero_rc_exits_nonzero_and_writes_nothing(tmp_path: Path) -> None:
    """An unrecordable build is NOT a successful one: a real build that
    finished (rc=0) but whose budget file went missing mid-run must not be
    reported as fine by exiting 0 with no evidence either way."""
    root = tmp_path / "root"
    (root / "ops").mkdir(parents=True)
    proc = _run(root, 60, 0)
    assert proc.returncode != 0
    assert not (root / "ops" / "logs" / "ship-dmg.log").exists()


def test_unreadable_budget_and_nonzero_rc_does_not_escalate(tmp_path: Path) -> None:
    """The build already failed; the recording step must not also fail,
    which would mask the real rc behind a recording-layer exit code."""
    root = tmp_path / "root"
    (root / "ops").mkdir(parents=True)
    proc = _run(root, 60, 1)
    assert proc.returncode == 0
    assert not (root / "ops" / "logs" / "ship-dmg.log").exists()
