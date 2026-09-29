"""Direct contract tests for ops/dmg-smoke/record_build_time.sh (DEVOPS-04, PR #4481).

This is the ONE executable path that writes a `phase=build_total` verdict to
ops/logs/ship-dmg.log: the justfile `dmg` recipe's EXIT trap delegates to it,
and the dmg-smoke test harness's headless build shim
(tests/scripts/dmg_smoke_harness.py) invokes this SAME file, so the two
cannot drift apart the way a hand-duplicated or regex-extracted copy of its
format string could (review round 2, P1: "factor the writer into an
executable production path and exercise that instead").

Budget path and log root are separate arguments on purpose, matching the
justfile's own independent `_budget`/`_root` variables: this is what lets
tests/scripts/test_build_budget.py's `_run_recorder` point the log root at a
throwaway tmp_path while passing the REAL ops/build-budget.env directly,
without needing a full checkout under tmp_path (review round 3, P1: "keep
the existing recorder contract tests runnable").

An optional 5th run_id argument stamps the row so a reader of a log shared
across builds (ops/dmg-smoke/run.sh's BUILD_WORKTREE) can prove a row
belongs to its own run rather than trusting log position alone (review
round 4, P1: "correlate the timing row with this build").

[if] record_build_time.sh runs [then] it appends the verdict its inputs predict, [else stop].
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.scripts.dmg_smoke_harness import REAL_BUILD_BUDGET, RECORD_BUILD_TIME

pytestmark = pytest.mark.requirement("DEVOPS-04")


def _run(
    budget: Path, root: Path, elapsed: int, rc: int, run_id: str | None = None
) -> subprocess.CompletedProcess[str]:
    argv = [str(RECORD_BUILD_TIME), str(budget), str(root), str(elapsed), str(rc)]
    if run_id is not None:
        argv.append(run_id)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _log_line(root: Path) -> str:
    return (root / "ops" / "logs" / "ship-dmg.log").read_text(encoding="utf-8").strip()


def test_under_soft_budget_reads_ok(tmp_path: Path) -> None:
    proc = _run(REAL_BUILD_BUDGET, tmp_path, 60, 0)
    assert proc.returncode == 0
    assert "phase=build_total seconds=60 rc=0 verdict=OK" in _log_line(tmp_path)


def test_omitted_run_id_defaults_to_none(tmp_path: Path) -> None:
    proc = _run(REAL_BUILD_BUDGET, tmp_path, 60, 0)
    assert proc.returncode == 0
    assert "run_id=none phase=build_total" in _log_line(tmp_path)


def test_empty_run_id_also_defaults_to_none(tmp_path: Path) -> None:
    proc = _run(REAL_BUILD_BUDGET, tmp_path, 60, 0, run_id="")
    assert proc.returncode == 0
    assert "run_id=none phase=build_total" in _log_line(tmp_path)


def test_a_given_run_id_is_stamped_onto_the_row(tmp_path: Path) -> None:
    proc = _run(REAL_BUILD_BUDGET, tmp_path, 60, 0, run_id="20260101T000000Z-4242")
    assert proc.returncode == 0
    assert "run_id=20260101T000000Z-4242 phase=build_total" in _log_line(tmp_path)


def test_over_soft_under_hard_reads_flag_soft(tmp_path: Path) -> None:
    # Real BUILD_TOTAL_SOFT_S=1320, BUILD_TOTAL_HARD_S=2700.
    proc = _run(REAL_BUILD_BUDGET, tmp_path, 1400, 0)
    assert proc.returncode == 0
    assert "seconds=1400 rc=0 verdict=FLAG_SOFT" in _log_line(tmp_path)


def test_over_hard_budget_reads_flag_hard(tmp_path: Path) -> None:
    proc = _run(REAL_BUILD_BUDGET, tmp_path, 3000, 0)
    assert proc.returncode == 0
    assert "seconds=3000 rc=0 verdict=FLAG_HARD" in _log_line(tmp_path)


def test_nonzero_rc_appends_failed_suffix_exactly_once(tmp_path: Path) -> None:
    proc = _run(REAL_BUILD_BUDGET, tmp_path, 60, 1)
    assert proc.returncode == 0
    line = _log_line(tmp_path)
    assert "verdict=OK_FAILED" in line
    assert "OK_FAILED_FAILED" not in line


def test_appends_rather_than_overwrites(tmp_path: Path) -> None:
    _run(REAL_BUILD_BUDGET, tmp_path, 60, 0)
    _run(REAL_BUILD_BUDGET, tmp_path, 70, 0)
    lines = (tmp_path / "ops" / "logs" / "ship-dmg.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "seconds=60" in lines[0]
    assert "seconds=70" in lines[1]


def test_unreadable_budget_and_zero_rc_exits_nonzero_and_writes_nothing(tmp_path: Path) -> None:
    """An unrecordable build is NOT a successful one: a real build that
    finished (rc=0) but whose budget file went missing mid-run must not be
    reported as fine by exiting 0 with no evidence either way."""
    proc = _run(tmp_path / "gone.env", tmp_path, 60, 0)
    assert proc.returncode != 0
    assert not (tmp_path / "ops" / "logs" / "ship-dmg.log").exists()


def test_unreadable_budget_and_nonzero_rc_does_not_escalate(tmp_path: Path) -> None:
    """The build already failed; the recording step must not also fail,
    which would mask the real rc behind a recording-layer exit code."""
    proc = _run(tmp_path / "gone.env", tmp_path, 60, 1)
    assert proc.returncode == 0
    assert not (tmp_path / "ops" / "logs" / "ship-dmg.log").exists()
