"""PERF-CAPTURE-11886: S7 library filter keystroke p95 from de-duplicated perf event rows.

[if] the p95 is not nearest-rank over pooled keystroke_ms, or under 30 keystrokes yield a number [then] fail, [else stop].
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.perf.capture_kpi_ledger import CaptureMeta
from scripts.perf.capture_s7_agg import (
    S7_DROPPED_KPI,
    S7_LATENCY_KPI,
    keystroke_latencies,
    snapshots_to_ledger_rows,
)
from scripts.perf.capture_s8_agg import unique_rows_of_kind

pytestmark = [pytest.mark.requirement("PERF-CAPTURE-11886")]

META = CaptureMeta(capture_id="perf-capture-test", date="2026-11-10", machine="test", sha="abc123")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _row(
    index: int, keystroke_ms: list[float], *, dropped: int = 0, keystrokes: int | None = None
) -> dict[str, object]:
    ks = keystrokes if keystrokes is not None else len(keystroke_ms) + dropped
    stage_bits = f"keystrokes={ks} dropped={dropped}"
    series_bits = f"keystroke_ms={','.join(str(int(x)) for x in keystroke_ms)}"
    return {
        "t": f"2026-11-10T08:00:{index:02d}.000Z",
        "kind": "library-filter",
        "deck": None,
        "message": f"{stage_bits} {series_bits}",
        "severity": "info",
        "stages": {"keystrokes": ks, "dropped": dropped},
        "series": {"keystroke_ms": keystroke_ms},
    }


def test_forty_latencies_give_p95_380_and_dropped_sum() -> None:
    """[if] 8 rows x 5 latencies 10..400 with dropped spread [then] p95 is 380 and dropped sums to 3."""
    latencies_per_row = [
        [10.0, 20.0, 30.0, 40.0, 50.0],
        [60.0, 70.0, 80.0, 90.0, 100.0],
        [110.0, 120.0, 130.0, 140.0, 150.0],
        [160.0, 170.0, 180.0, 190.0, 200.0],
        [210.0, 220.0, 230.0, 240.0, 250.0],
        [260.0, 270.0, 280.0, 290.0, 300.0],
        [310.0, 320.0, 330.0, 340.0, 350.0],
        [360.0, 370.0, 380.0, 390.0, 400.0],
    ]
    dropped = [0, 2, 0, 1, 0, 0, 0, 0]
    snapshot = {"perf_events": [_row(i, latencies_per_row[i], dropped=dropped[i]) for i in range(8)]}
    rows = snapshots_to_ledger_rows([snapshot], META)
    assert len(rows) == 2
    assert (rows[0]["kpi"], rows[0]["value"], rows[0]["unit"]) == (S7_LATENCY_KPI, 380.0, "ms p95")
    assert "status" not in rows[0]
    assert (rows[1]["kpi"], rows[1]["value"], rows[1]["unit"]) == (
        S7_DROPPED_KPI,
        3.0,
        "dropped keystrokes per session",
    )


def test_overlapping_snapshot_counts_once() -> None:
    """[if] the same row is in two snapshots [then] latencies and dropped count once."""
    row = _row(0, [10.0, 20.0, 30.0], dropped=1, keystrokes=4)
    rows = snapshots_to_ledger_rows([{"perf_events": [row]}, {"perf_events": [row]}], META)
    assert rows[0]["status"] == "withheld"
    assert rows[1]["status"] == "withheld"


def test_twenty_nine_latencies_are_withheld() -> None:
    """[if] only 29 latencies are present [then] both rows are withheld naming 29 of 30."""
    snapshot = [_row(i, [float(10 + i)]) for i in range(29)]
    rows = snapshots_to_ledger_rows([snapshot], META)
    assert rows[0]["status"] == "withheld"
    assert rows[0]["value"] is None
    assert "29 of 30" in rows[0]["note"]
    assert rows[1]["status"] == "withheld"


def test_legacy_row_without_series_is_skipped() -> None:
    """[if] a row has no series [then] it is skipped without error."""
    legacy = {**_row(0, [5.0]), "series": None}
    del legacy["series"]
    rows = unique_rows_of_kind([[legacy] + [_row(i, [10.0]) for i in range(1, 30)]], "library-filter")
    assert len(rows) == 30
    out = snapshots_to_ledger_rows([[legacy] + [_row(i, [10.0 + i]) for i in range(1, 30)]], META)
    assert out[0]["status"] == "withheld"


def test_invalid_keystroke_ms_raises() -> None:
    """[if] a latency is negative or non-numeric [then] ValueError."""
    bad = {**_row(0, [1.0]), "series": {"keystroke_ms": [-1.0]}}
    with pytest.raises(ValueError, match="keystroke_ms"):
        keystroke_latencies(bad)


def test_dry_run_prints_both_kpis(tmp_path: Path) -> None:
    """[if] the CLI runs with --dry-run [then] stdout names both S7 KPIs."""
    latencies_per_row = [[float(10 + i * 5 + j) for j in range(5)] for i in range(8)]
    snapshot = {"perf_events": [_row(i, latencies_per_row[i]) for i in range(8)]}
    samples = tmp_path / "snapshots.json"
    samples.write_text(json.dumps([snapshot]))
    ledger = tmp_path / "ledger.json"
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.perf.capture_s7_agg",
            "--samples",
            str(samples),
            "--ledger",
            str(ledger),
            "--dry-run",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert S7_LATENCY_KPI in done.stdout
    assert S7_DROPPED_KPI in done.stdout
    assert not ledger.exists()
