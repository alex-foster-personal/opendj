"""PERF-CAPTURE-9695: S1 xruns and silent-output rates per playing hour from client samples.

[if] idle xruns count, a probe-off capture reads 0, or a short capture scores [then] fail, [else stop].
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.perf import capture_s1_agg as s1
from scripts.perf.capture_kpi_ledger import CaptureMeta

pytestmark = [pytest.mark.requirement("PERF-CAPTURE-9695")]

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
META = CaptureMeta(capture_id="s1-test", date="2026-11-13", machine="test", sha="0" * 40)
WINDOWS_PER_MINUTE = 6


def _sample(
    index: int,
    *,
    playing: bool,
    xruns: int | None = 0,
    probe: bool = True,
    audible: bool | None = None,
    level: str = "ok",
    session: str = "s",
) -> dict[str, Any]:
    """One stored client performance sample with deck 1 playing or idle and a 10 s probe window."""
    decks = [
        {
            "deck_id": deck,
            "playing": playing and deck == 1,
            "audible": (playing if audible is None else audible) and deck == 1,
            "transport_pending": False,
        }
        for deck in (1, 2, 3, 4)
    ]
    window = {"shell": "browser", "window_ms": 10_000.0, "frames": 600, "xruns": xruns} if probe else None
    return {
        "kind": "client-performance-sample",
        "client_session_id": session,
        "client_timestamp": f"2026-11-13T10:{index // 6:02d}:{(index % 6) * 10:02d}.000Z",
        "audio_health_level": level if playing else "idle",
        "decks": decks,
        "frame_probe": window,
    }


def _session(playing_minutes: int, *, idle_minutes: int = 0, xruns: int | None = 0) -> list[dict[str, Any]]:
    """Playing windows followed by idle windows, every window with ``xruns`` xruns."""
    playing = [_sample(i, playing=True, xruns=xruns) for i in range(playing_minutes * WINDOWS_PER_MINUTE)]
    offset = len(playing)
    idle = [_sample(offset + i, playing=False, xruns=xruns) for i in range(idle_minutes * WINDOWS_PER_MINUTE)]
    return playing + idle


def _rows_by_kpi(samples: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The S1 rows keyed by KPI name."""
    return {row["kpi"]: row for row in s1.samples_to_ledger_rows(samples, META)}


def test_idle_xruns_are_not_counted() -> None:
    """[if] 2 playing xruns over 30 minutes plus 5 idle xruns is not 4.0 per hour [then] fail, [else stop]."""
    samples = _session(30, idle_minutes=5)
    samples[3]["frame_probe"]["xruns"] = 1
    samples[100]["frame_probe"]["xruns"] = 1
    for index in (181, 185, 190, 195, 200):
        samples[index]["frame_probe"]["xruns"] = 1
    row = _rows_by_kpi(samples)[s1.XRUN_KPI]
    assert row["value"] == 4.0
    assert row.get("status") is None
    assert "2 xruns over 30.0" in row["note"]


def test_probe_off_withholds_and_never_reads_zero() -> None:
    """[if] all-null xruns windows score a number [then] fail, [else stop]."""
    row = _rows_by_kpi(_session(30, xruns=None))[s1.XRUN_KPI]
    assert row["value"] is None
    assert row["status"] == "withheld"
    assert "frame probe off" in row["note"]


def test_no_probe_window_at_all_also_names_probe_off() -> None:
    """[if] samples without a frame_probe score xruns [then] fail, [else stop]."""
    samples = [_sample(i, playing=True, probe=False) for i in range(30 * WINDOWS_PER_MINUTE)]
    rows = _rows_by_kpi(samples)
    assert "frame probe off" in rows[s1.XRUN_KPI]["note"]
    assert rows[s1.SILENT_KPI]["value"] == 0.0


def test_under_ten_playing_minutes_withholds_naming_the_minutes() -> None:
    """[if] 9 playing minutes score either KPI [then] fail, [else stop]."""
    rows = _rows_by_kpi(_session(9, idle_minutes=20))
    for kpi in (s1.XRUN_KPI, s1.SILENT_KPI):
        assert rows[kpi]["status"] == "withheld"
        assert rows[kpi]["value"] is None
        assert "9.0 playing minutes" in rows[kpi]["note"]


def test_silent_and_stalled_rising_edges_are_events_per_playing_hour() -> None:
    """[if] a sustained silence counts per sample or a stall is missed [then] fail, [else stop]."""
    samples = _session(30)
    for index in (10, 11, 12, 50):
        samples[index]["decks"][0]["audible"] = False
    samples[70]["audio_health_level"] = "crit"
    samples[71]["audio_health_level"] = "crit"
    row = _rows_by_kpi(samples)[s1.SILENT_KPI]
    assert row["value"] == 6.0
    assert "2 silent-while-playing + 1 presentation-stalled" in row["note"]


def test_transport_pending_silence_is_not_an_event() -> None:
    """[if] a deck waiting on a scheduled start counts as silent [then] fail, [else stop]."""
    samples = _session(30)
    samples[5]["decks"][0].update(audible=False, transport_pending=True)
    assert _rows_by_kpi(samples)[s1.SILENT_KPI]["value"] == 0.0


def test_dry_run_prints_both_s1_kpis(tmp_path: Path) -> None:
    """[if] the CLI dry run does not print both S1 KPI rows from JSONL [then] fail, [else stop]."""
    path = tmp_path / "client-samples.jsonl"
    path.write_text("\n".join(json.dumps(sample) for sample in _session(30)) + "\n")
    result = subprocess.run(
        [sys.executable, "-m", "scripts.perf.capture_s1_agg", "--samples", str(path), "--dry-run"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [row["kpi"] for row in rows] == [s1.XRUN_KPI, s1.SILENT_KPI]
    assert [row["value"] for row in rows] == [0.0, 0.0]


def test_s1_kpis_are_the_required_ones_in_the_kpi_map() -> None:
    """[if] the aggregator's KPI names drift from S1 required in kpi-map.json [then] fail, [else stop]."""
    kpi_map = json.loads((REPOSITORY_ROOT / "docs" / "perf" / "kpi-map.json").read_text())
    scenario = kpi_map["scenarios"]["S1"]
    assert scenario["required"] == [s1.XRUN_KPI, s1.SILENT_KPI]
    assert "capture_s1_agg" in scenario["missing_kpi"]
