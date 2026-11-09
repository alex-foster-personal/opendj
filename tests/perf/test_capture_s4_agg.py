"""PERF-CAPTURE-9962: S4 waveform KPIs come from frame probe windows taken while a deck plays.

[if] dropped frames use a fixed 16.7 ms, max is a percentile, or idle windows count [then] fail, [else stop].
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.perf import capture_s4_agg as s4
from scripts.perf.capture_kpi_ledger import CaptureMeta

pytestmark = [pytest.mark.requirement("PERF-CAPTURE-9962")]

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
META = CaptureMeta(capture_id="s4-test", date="2026-11-08", machine="test", sha="0" * 40)


def _window(*, hz: float, p95_ms: float, max_ms: float, p50_ms: float | None = None, long_frames: int = 0) -> dict[str, Any]:
    frames = int(hz * 10)
    interval = 1000.0 / hz
    return {
        "shell": "browser", "window_ms": 10_000.0, "frames": frames, "frames_sampled": frames,
        "fps": float(hz), "p50_ms": round(interval, 1) if p50_ms is None else p50_ms, "p95_ms": p95_ms,
        "p99_ms": max_ms, "max_ms": max_ms, "long_frames": long_frames, "xruns": None,
    }


def _sample(window: dict[str, Any] | None, *, playing: bool) -> dict[str, Any]:
    decks = [{"deck_id": deck, "playing": playing and deck == 1} for deck in (1, 2, 3, 4)]
    return {"kind": "client-performance-sample", "decks": decks, "frame_probe": window}


def _rows_by_kpi(samples: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["kpi"]: row for row in s4.samples_to_ledger_rows(samples, META)}


def test_a_12ms_frame_is_dropped_at_120hz_but_not_at_60hz() -> None:
    """[if] a 12 ms frame scores the same at 120 Hz and 60 Hz [then] fail, [else stop]."""
    at_120 = s4.score_window(_window(hz=120, p95_ms=8.4, max_ms=12.0))
    at_60 = s4.score_window(_window(hz=60, p95_ms=16.7, max_ms=12.0))
    assert at_120 is not None and at_60 is not None
    assert at_120.refresh_interval_ms == pytest.approx(8.333, abs=0.001)
    assert at_120.dropped_frames >= 1
    assert at_60.dropped_frames == 0
    assert at_60.dropped_pct == 0


def test_a_p95_over_the_line_proves_at_least_five_percent_dropped() -> None:
    """[if] p95 above the drop line proves fewer than 5% dropped [then] fail, [else stop]."""
    score = s4.score_window(_window(hz=120, p95_ms=12.0, max_ms=20.0))
    assert score is not None
    assert score.dropped_pct >= 5.0


def test_max_is_the_single_worst_max_ms_across_playing_windows() -> None:
    """[if] waveform_frame_delta_ms_max is not the single worst max_ms [then] fail, [else stop]."""
    samples = [
        _sample(_window(hz=60, p95_ms=16.7, max_ms=20.0), playing=True),
        _sample(_window(hz=60, p95_ms=16.8, max_ms=1250.0), playing=True),
        _sample(_window(hz=60, p95_ms=16.6, max_ms=40.0), playing=True),
    ]
    rows = _rows_by_kpi(samples)
    assert rows[s4.MAX_KPI]["value"] == 1250.0
    assert rows[s4.P95_KPI]["value"] == 16.8
    assert set(rows) == {s4.P95_KPI, s4.DROPPED_KPI, s4.MAX_KPI}


def test_windows_with_no_deck_playing_are_excluded() -> None:
    """[if] an idle window's freeze reaches the S4 rows [then] fail, [else stop]."""
    samples = [
        _sample(_window(hz=60, p95_ms=16.7, max_ms=20.0), playing=True),
        _sample(_window(hz=60, p95_ms=500.0, max_ms=5000.0), playing=False),
    ]
    rows = _rows_by_kpi(samples)
    assert rows[s4.MAX_KPI]["value"] == 20.0
    assert rows[s4.P95_KPI]["value"] == 16.7


def test_zero_playing_windows_yields_withheld_rows_with_the_reason() -> None:
    """[if] no playing window writes a number instead of a withheld row with a reason [then] fail, [else stop]."""
    samples = [_sample(_window(hz=60, p95_ms=16.7, max_ms=20.0), playing=False), _sample(None, playing=True)]
    rows = _rows_by_kpi(samples)
    assert set(rows) == {s4.P95_KPI, s4.DROPPED_KPI, s4.MAX_KPI}
    for row in rows.values():
        assert row["value"] is None
        assert row["status"] == "withheld"
        assert "no frame probe window was taken while a deck was playing" in row["note"]


def test_dry_run_prints_the_three_s4_kpis(tmp_path: Path) -> None:
    """[if] --samples <file> --dry-run does not print the three S4 KPIs [then] fail, [else stop]."""
    samples_path = tmp_path / "client-samples.json"
    samples_path.write_text(json.dumps([_sample(_window(hz=120, p95_ms=8.4, max_ms=12.0), playing=True)]))
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.perf.capture_s4_agg", "--samples", str(samples_path), "--dry-run"],
        cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=False, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    rows = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [row["kpi"] for row in rows] == [s4.P95_KPI, s4.DROPPED_KPI, s4.MAX_KPI]
    assert all(isinstance(row["value"], float) for row in rows)


def test_without_dry_run_the_command_refuses(tmp_path: Path) -> None:
    """[if] the aggregator runs without --dry-run and exits 0 [then] fail, [else stop]."""
    samples_path = tmp_path / "client-samples.json"
    samples_path.write_text("[]")
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.perf.capture_s4_agg", "--samples", str(samples_path)],
        cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=False, timeout=60,
    )
    assert completed.returncode != 0
    assert "--dry-run" in completed.stderr
