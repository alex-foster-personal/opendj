"""PERF-CAPTURE-9701: the S10 row-select prefetch ready p95 comes from de-duplicated sampled prefetch rows.

[if] the p95 is not nearest-rank seconds over unique ready rows, ok=0 rows count, or under 10 rows give a number [then] fail, [else stop].
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.perf.capture_kpi_ledger import CaptureMeta
from scripts.perf.capture_s10_agg import S10_KPI, snapshots_to_ledger_row, unique_prefetch_rows

pytestmark = [pytest.mark.requirement("PERF-CAPTURE-9701")]

META = CaptureMeta(capture_id="perf-capture-test", date="2026-11-13", machine="test", sha="abc123")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _anlz(index: int, fetch_ms: int, *, ok: int = 1) -> dict[str, object]:
    """One synthetic library-prefetch-anlz timing row as recordAnlzPrefetchSampled writes it."""
    return {
        "t": f"2026-11-13T21:00:{index:02d}.000Z",
        "kind": "library-prefetch-anlz",
        "deck": None,
        "message": f"fetch_ms={fetch_ms} ok={ok} sample_of=5",
        "severity": "info",
        "stages": {"fetch_ms": fetch_ms, "ok": ok, "sample_of": 5},
    }


def _audio(index: int, fetch_ms: int) -> dict[str, object]:
    """One synthetic library-prefetch-audio timing row as recordAudioPrefetchSampled writes it."""
    return {
        "t": f"2026-11-13T21:01:{index:02d}.000Z",
        "kind": "library-prefetch-audio",
        "deck": None,
        "message": f"fetch_ms={fetch_ms} mib=9 sample_of=5",
        "severity": "info",
        "stages": {"fetch_ms": fetch_ms, "mib": 9, "sample_of": 5},
    }


def _twenty_anlz() -> list[dict[str, object]]:
    """Twenty anlz rows evenly spaced from 100 to 2000 ms."""
    return [_anlz(i, 100 * (i + 1)) for i in range(20)]


def test_twenty_anlz_rows_of_100_to_2000_ms_give_a_p95_of_1_9_s() -> None:
    """[if] 20 anlz rows run 100 to 2000 ms [then] row_select_prefetch_ready_s is 1.9 s."""
    row = snapshots_to_ledger_row([{"perf_events": _twenty_anlz()}], META)
    assert (row["kpi"], row["value"], row["unit"]) == (S10_KPI, 1.9, "s")
    assert "status" not in row
    assert "anlz p95 1.9s (n=20)" in row["note"]
    assert "audio p95 n/a (n=0)" in row["note"]


def test_ok_zero_rows_are_excluded_and_reported_as_an_error_rate() -> None:
    """[if] slow rows carry ok=0 [then] they leave the p95 and show up as the error rate."""
    failed = [_anlz(30 + i, 60_000, ok=0) for i in range(5)]
    row = snapshots_to_ledger_row([{"perf_events": _twenty_anlz() + failed}], META)
    assert row["value"] == 1.9
    assert "error rate 5/25 (20.0%)" in row["note"]


def test_method_states_the_sampling_factor() -> None:
    """[if] rows declare sample_of=5 [then] the method says sampled 1 in 5."""
    row = snapshots_to_ledger_row([_twenty_anlz()], META)
    assert "sampled 1 in 5" in row["method"]


def test_nine_ready_rows_are_withheld() -> None:
    """[if] fewer than 10 ready rows are present [then] the row is withheld, never a number."""
    rows = [_anlz(i, 200) for i in range(5)] + [_audio(i, 300) for i in range(4)] + [_anlz(40, 9, ok=0)]
    row = snapshots_to_ledger_row([rows], META)
    assert row["status"] == "withheld"
    assert row["value"] is None
    assert "9 of 10" in row["note"]
    assert "sampled 1 in 5" in row["method"]


def test_overlapping_snapshots_count_each_event_once_across_kinds() -> None:
    """[if] the same events appear in two snapshots [then] each counts once and both kinds feed the p95."""
    first = {"perf_events": [_anlz(0, 100), _audio(0, 400)]}
    second = {"perf_events": [_anlz(0, 100), _audio(0, 400), {**_anlz(1, 999), "kind": "deck-load"}]}
    by_kind = unique_prefetch_rows([first, second])
    assert [len(by_kind[k]) for k in ("library-prefetch-anlz", "library-prefetch-audio")] == [1, 1]
    row = snapshots_to_ledger_row([[_anlz(i, 100) for i in range(9)], [_audio(0, 3000)], [_audio(0, 3000)]], META)
    assert row["value"] == 3.0
    assert "n=10 ready rows" in row["note"]


def test_dry_run_prints_the_p95(tmp_path: Path) -> None:
    """[if] the CLI runs with --dry-run [then] it prints the p95 in seconds and appends nothing."""
    samples = tmp_path / "snapshots.json"
    samples.write_text(json.dumps([{"perf_events": _twenty_anlz()}]))
    ledger = tmp_path / "ledger.json"
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.perf.capture_s10_agg",
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
    assert f"{S10_KPI}  1.9 s" in done.stdout
    assert not ledger.exists()
