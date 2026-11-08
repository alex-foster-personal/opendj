"""PERF-LEAK-10038 per-role memory slope (issue #9697).

[if] a role memory slope is misreported, moved by churn outside idle windows, or printed for a thin window [then] fail, [else stop].
[if] a role growing 60 MB over an idle hour does not report 60.0 and exit 1, churn outside
the idle windows moves a slope, or a thin window prints a slope [then] fail, [else stop].
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.perf.role_memory_slope import INSUFFICIENT, main, parse_idle_window, read_probe_rows, role_slopes

pytestmark = [pytest.mark.requirement("PERF-LEAK-10038")]

T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
WINDOW = "2026-11-08T10:00:00Z,2026-11-08T11:00:00Z"


def _row(minutes: float, engine_mb: float, shell_mb: float = 300.0) -> dict:
    return {
        "timestamp": (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
        "processes": [
            {"role": "python-engine", "pid": 10, "physical_footprint_mb": engine_mb},
            {"role": "desktop-shell", "pid": 11, "physical_footprint_mb": shell_mb},
            {"role": "webkit-webcontent", "pid": 12, "physical_footprint_mb": 400.0},
            {"role": "webkit-webcontent", "pid": 13, "physical_footprint_mb": 100.0},
        ],
    }


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "probe.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def _leaking_hour() -> list[dict]:
    return [_row(minute, 1000.0 + minute) for minute in range(0, 61, 5)]


def test_sixty_mb_per_idle_hour_reports_60_and_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] python-engine grows 60 MB over one idle hour, others flat [then] slope 60.0, exit 1."""
    log = _write(tmp_path, _leaking_hour())
    assert main(["--log", str(log), "--idle-window", WINDOW, "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["python-engine"]["slope_mb_per_hour"] == 60.0
    assert out["python-engine"]["first_mb"] == 1000.0 and out["python-engine"]["last_mb"] == 1060.0
    assert out["python-engine"]["points"] == 13
    assert out["desktop-shell"]["slope_mb_per_hour"] == 0.0
    assert out["webkit-webcontent"]["first_mb"] == 500.0  # members sharing a role are summed


def test_flat_roles_exit_zero_in_text_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] every role is flat [then] exit 0 and one text line per role."""
    log = _write(tmp_path, [_row(minute, 1000.0) for minute in range(0, 61, 10)])
    assert main(["--log", str(log), "--idle-window", WINDOW]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert [line.split()[0] for line in lines] == ["desktop-shell", "python-engine", "webkit-webcontent"]
    assert all("slope 0.0 MB/h" in line for line in lines)


def test_rows_outside_idle_windows_are_ignored() -> None:
    """[if] a 500 MB jump happens during churn between two windows [then] no slope moves."""
    first = [_row(minute, 1000.0) for minute in range(0, 31, 5)]
    churn = [_row(minute, 1500.0) for minute in range(35, 60, 5)]
    second = [_row(minute, 1500.0) for minute in range(60, 91, 5)]
    windows = [
        parse_idle_window("2026-11-08T10:00:00Z,2026-11-08T10:30:00Z"),
        parse_idle_window("2026-11-08T11:00:00Z,2026-11-08T11:30:00Z"),
    ]
    slopes = {s.role: s for s in role_slopes(first + churn + second, windows)}
    engine = slopes["python-engine"]
    assert [w.slope_mb_per_hour for w in engine.windows] == [0.0, 0.0]
    assert engine.slope_mb_per_hour == 0.0
    assert engine.points == 14


def test_thin_window_reports_insufficient_samples(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] a window has fewer than 3 points for a role [then] it prints insufficient samples."""
    log = _write(tmp_path, [_row(0, 1000.0), _row(30, 1200.0)])
    assert main(["--log", str(log), "--idle-window", WINDOW]) == 0
    out = capsys.readouterr().out
    assert out.count(INSUFFICIENT) == 3
    assert "MB/h" not in out


def test_bad_inputs_fail_fast(tmp_path: Path) -> None:
    """[if] a window is reversed or a log line is not JSON [then] ValueError naming it."""
    with pytest.raises(ValueError, match="end must be after start"):
        parse_idle_window("2026-11-08T11:00:00Z,2026-11-08T10:00:00Z")
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"timestamp": "2026-11-08T10:00:00Z"}\nnot json\n')
    with pytest.raises(ValueError, match=r"bad.jsonl:2"):
        read_probe_rows(bad)
