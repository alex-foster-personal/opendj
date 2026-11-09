"""PERF-CAPTURE-9959: the S8 search round trip p95 comes from de-duplicated perf event rows.

[if] the p95 is not nearest-rank over unique search rows, or under 20 queries yield a number [then] fail, [else stop].
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.perf.capture_kpi_ledger import CaptureMeta
from scripts.perf.capture_s8_agg import S8_KPI, snapshots_to_ledger_row, unique_search_rows

pytestmark = [pytest.mark.requirement("PERF-CAPTURE-9959")]

META = CaptureMeta(capture_id="perf-capture-test", date="2026-11-08", machine="test", sha="abc123")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _row(index: int, query_ms: float) -> dict[str, object]:
    return {
        "t": f"2026-11-08T21:00:{index:02d}.000Z",
        "kind": "library-search-collection",
        "deck": None,
        "message": f"query_ms={query_ms} hits=10 total=40",
        "severity": "info",
        "stages": {"query_ms": query_ms, "hits": 10, "total": 40},
    }


def test_forty_rows_of_1_to_40_ms_give_a_p95_of_38() -> None:
    """[if] 40 fixture rows carry query_ms 1 to 40 [then] collection_search_round_trip_ms_p95 is 38."""
    snapshot = {"perf_events": [_row(i, float(i + 1)) for i in range(40)]}
    row = snapshots_to_ledger_row([snapshot], META)
    assert (row["kpi"], row["value"], row["unit"]) == (S8_KPI, 38.0, "ms")
    assert "status" not in row


def test_an_event_in_two_overlapping_samples_counts_once() -> None:
    """[if] the same event is in two overlapping snapshots [then] it is counted once."""
    first = {"perf_events": [_row(0, 5.0), _row(1, 6.0)]}
    second = {"perf_events": [_row(1, 6.0), _row(2, 7.0)]}
    rows = unique_search_rows([first, second])
    assert [r["stages"]["query_ms"] for r in rows] == [5.0, 6.0, 7.0]


def test_other_kinds_are_ignored_and_ids_win_over_rows() -> None:
    """[if] a snapshot holds other event kinds or rows with ids [then] only search rows count, by id."""
    other = {**_row(3, 9.0), "kind": "deck-load"}
    with_id = {**_row(4, 9.0), "id": "e1"}
    same_id_new_time = {**_row(5, 9.0), "id": "e1"}
    rows = unique_search_rows([[other, with_id], [same_id_new_time]])
    assert len(rows) == 1


def test_nineteen_rows_are_withheld_naming_19_of_20() -> None:
    """[if] 19 rows are present [then] the row is withheld naming 19 of 20 queries."""
    row = snapshots_to_ledger_row([[_row(i, 10.0) for i in range(19)]], META)
    assert row["status"] == "withheld"
    assert row["value"] is None
    assert "19 of 20 queries" in row["note"]


def test_a_row_without_query_ms_raises() -> None:
    """[if] a search row has no finite query_ms [then] ValueError, never a guessed value."""
    broken = {**_row(0, 1.0), "stages": {"hits": 1}}
    with pytest.raises(ValueError, match="query_ms"):
        snapshots_to_ledger_row([[broken] + [_row(i, 1.0) for i in range(1, 25)]], META)


def test_dry_run_prints_the_p95(tmp_path: Path) -> None:
    """[if] the CLI runs with --dry-run [then] it prints the p95 and appends nothing."""
    samples = tmp_path / "snapshots.json"
    samples.write_text(json.dumps([{"perf_events": [_row(i, float(i + 1)) for i in range(40)]}]))
    ledger = tmp_path / "ledger.json"
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.perf.capture_s8_agg",
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
    assert f"{S8_KPI}  38.0 ms" in done.stdout
    assert not ledger.exists()
