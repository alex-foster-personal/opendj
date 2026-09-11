"""S2 press-to-audible KPI capture writer and CLI tests."""

from __future__ import annotations

import datetime as _dt
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.perf.capture_kpi_ledger import CaptureMeta
from scripts.perf.capture_kpis import main as capture_kpis_main
from scripts.perf.capture_s2_agg import (
    counts_as_sample,
    nearest_rank_p99,
    presses_to_ledger_rows,
)
from scripts.perf.kpi_scorecard import UNMEASURED, render, score_scenarios

_REPO = Path(__file__).resolve().parents[2]


def _meta() -> CaptureMeta:
    return CaptureMeta(
        capture_id="perf-capture-20260911T151205Z",
        date="2026-09-11",
        machine="test-host",
        sha="abc123def",
    )


def _complete_press(input_to_output_ms: float, *, output_latency_ms: float = 15.964) -> dict:
    base_latency_ms = round(output_latency_ms * 0.18 + 2.902, 3)
    return {
        "kind": "transport-schedule-press",
        "deck": 1,
        "stages": {
            "press_to_schedule_ms": 1.2,
            "scheduled_offset_ms": 8.0,
            "input_to_audible_ms": round(
                input_to_output_ms - base_latency_ms - output_latency_ms + 8.0,
                3,
            ),
            "input_to_output_ms": input_to_output_ms,
            "base_latency_ms": base_latency_ms,
            "output_latency_ms": output_latency_ms,
        },
        "labels": {"latency_floor": "complete"},
    }


def _happy_result(presses: list[dict], *, engine: str = "WebKit 26.5") -> dict:
    return {
        "ok": True,
        "engine": engine,
        "browser": "webkit",
        "presses": presses,
        "reason": None,
    }


def _closed_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _shipped_map() -> dict:
    return json.loads((_REPO / "docs" / "perf" / "kpi-map.json").read_text())


def _score_one(sid: str, entries: list[dict]):
    kpi_map = {"scenarios": {sid: _shipped_map()["scenarios"][sid]}}
    (score,) = score_scenarios(kpi_map, entries, _dt.date(2026, 9, 11))
    return score


@pytest.mark.requirement("PERF-KPI-S2")
def test_happy_fixture_writes_numeric_p99() -> None:
    """[if] a happy S2 capture completes [then] it writes a numeric p99 row, [else stop]."""
    presses = [_complete_press(10.0 + index) for index in range(32)]
    rows = presses_to_ledger_rows(_happy_result(presses), _meta(), requested_presses=32)
    audible = next(row for row in rows if row["kpi"] == "input_to_audible_ms_p99")
    assert audible["value"] == nearest_rank_p99([10.0 + index for index in range(32)])
    assert audible["unit"] == "ms"
    assert audible["machine"] == "test-host"
    assert "n=32" in audible["note"]
    assert "engine=WebKit 26.5" in audible["note"]
    assert "audio_output_device_floor_ms=" in audible["note"]
    assert audible.get("status") is None
    assert "perf-capture" in audible["capture_id"]


@pytest.mark.requirement("PERF-KPI-S2")
def test_partial_floor_is_not_a_number() -> None:
    """[if] every press row has a partial floor [then] the row is withheld, [else stop]."""
    presses = [
        {
            "kind": "transport-schedule-press",
            "deck": 1,
            "stages": {"scheduled_offset_ms": 8.0, "input_to_audible_ms": 20.0},
            "labels": {"latency_floor": "partial"},
        }
    ]
    rows = presses_to_ledger_rows(_happy_result(presses), _meta(), requested_presses=32)
    audible = next(row for row in rows if row["kpi"] == "input_to_audible_ms_p99")
    assert audible["value"] is None
    assert audible["status"] == "withheld"
    assert audible["measured"] is False
    encoded = json.dumps(rows)
    assert '"value": null' in encoded
    assert '"value": 0' not in encoded


@pytest.mark.requirement("PERF-KPI-S2")
def test_zero_floor_withheld() -> None:
    """[if] outputLatency reads zero [then] the row is withheld with a zero reason, [else stop]."""
    presses = [
        {
            "kind": "transport-schedule-press",
            "deck": 1,
            "stages": {
                "input_to_output_ms": 25.0,
                "base_latency_ms": 2.902,
                "output_latency_ms": 0.0,
            },
            "labels": {"latency_floor": "complete"},
        }
    ]
    rows = presses_to_ledger_rows(
        {
            "ok": False,
            "reason": "audio context had not rendered or outputLatency is 0",
            "presses": presses,
        },
        _meta(),
        requested_presses=32,
    )
    audible = next(row for row in rows if row["kpi"] == "input_to_audible_ms_p99")
    assert audible["value"] is None
    assert "rendered" in audible["note"] or "outputLatency is 0" in audible["note"]


@pytest.mark.requirement("PERF-KPI-S2")
def test_load_span_and_armed_kinds_excluded() -> None:
    """[if] load-span and armed rows are mixed in [then] they do not become the p99, [else stop]."""
    ordinary = [_complete_press(12.0 + index) for index in range(32)]
    noisy = [
        {
            "kind": "transport-schedule-press-load-span",
            "deck": 1,
            "stages": {
                "input_to_output_ms": 5000.0,
                "base_latency_ms": 2.9,
                "output_latency_ms": 16.0,
            },
            "labels": {"latency_floor": "complete"},
        },
        {
            "kind": "transport-schedule-press-armed",
            "deck": 1,
            "stages": {
                "input_to_output_ms": 4000.0,
                "base_latency_ms": 2.9,
                "output_latency_ms": 16.0,
            },
            "labels": {"latency_floor": "complete"},
        },
    ]
    rows = presses_to_ledger_rows(
        _happy_result(noisy + ordinary),
        _meta(),
        requested_presses=32,
    )
    audible = next(row for row in rows if row["kpi"] == "input_to_audible_ms_p99")
    assert audible["value"] == nearest_rank_p99([12.0 + index for index in range(32)])
    assert audible["value"] != 5000.0


@pytest.mark.requirement("PERF-KPI-S2")
def test_short_sample_withheld() -> None:
    """[if] fewer than N complete presses arrive [then] the row is withheld, [else stop]."""
    presses = [_complete_press(20.0 + index) for index in range(3)]
    rows = presses_to_ledger_rows(_happy_result(presses), _meta(), requested_presses=32)
    audible = next(row for row in rows if row["kpi"] == "input_to_audible_ms_p99")
    assert audible["value"] is None
    assert "fewer than 32" in audible["note"]


@pytest.mark.requirement("PERF-KPI-S2")
def test_unreachable_engine_appends_withheld_rows(tmp_path: Path) -> None:
    """[if] the engine is unreachable [then] capture_kpis appends withheld S2 rows, [else stop]."""
    port = _closed_local_port()
    ledger = tmp_path / "kpi-ledger.json"
    ledger.write_text(
        json.dumps({"schema_version": 1, "entries": []}, indent=1) + "\n",
        encoding="utf-8",
    )
    exit_code = capture_kpis_main(
        [
            "--engine",
            f"http://127.0.0.1:{port}",
            "--scenario",
            "S2",
            "--ledger",
            str(ledger),
        ]
    )
    assert exit_code == 1
    rows = json.loads(ledger.read_text(encoding="utf-8"))["entries"]
    audible = next(row for row in rows if row["kpi"] == "input_to_audible_ms_p99")
    assert audible["value"] is None
    assert audible["status"] == "withheld"


@pytest.mark.requirement("PERF-KPI-S2")
def test_cli_help_names_s2() -> None:
    """[if] capture_kpis prints help [then] it names S2, [else stop]."""
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.perf.capture_kpis", "--help"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "S2" in proc.stdout


@pytest.mark.requirement("PERF-KPI-S2")
def test_scorecard_render_after_numeric_row() -> None:
    """[if] a numeric audible row exists [then] render skips never recorded, [else stop]."""
    presses = [_complete_press(10.0 + index) for index in range(32)]
    rows = presses_to_ledger_rows(_happy_result(presses), _meta(), requested_presses=32)
    score = _score_one("S2", rows)
    assert score.verdict == UNMEASURED
    rendered = "\n".join(render([score], max_stale_days=None))
    assert "input_to_audible_ms_p99 = " in rendered
    assert "input_to_audible_ms_p99 = never recorded" not in rendered


@pytest.mark.requirement("PERF-KPI-S2")
def test_p99_math_pin() -> None:
    """[if] nearest-rank p99 is computed [then] it uses the pinned index formula, [else stop]."""
    values = [float(10 * (index + 1)) for index in range(10)]
    assert nearest_rank_p99(values) == 100.0


@pytest.mark.requirement("PERF-KPI-S2")
def test_does_not_score_scheduled_offset_ms() -> None:
    """[if] only scheduled_offset_ms is present [then] the row is withheld, [else stop]."""
    presses = [
        {
            "kind": "transport-schedule-press",
            "deck": 1,
            "stages": {"scheduled_offset_ms": 8.0},
            "labels": {"latency_floor": "complete"},
        }
    ]
    rows = presses_to_ledger_rows(_happy_result(presses), _meta(), requested_presses=32)
    audible = next(row for row in rows if row["kpi"] == "input_to_audible_ms_p99")
    assert audible["value"] is None
    assert not counts_as_sample(presses[0])
