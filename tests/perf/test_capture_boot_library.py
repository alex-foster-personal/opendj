"""BOOT-LIB capture writer tests (issue #2697, PERF-UI-03)."""

from __future__ import annotations

import datetime as _dt
import json

import pytest

from scripts.perf.capture_boot_library_ledger import span_to_ledger_rows
from scripts.perf.kpi_scorecard import score_scenarios

_REPO = __import__("pathlib").Path(__file__).resolve().parents[2]


def _shipped_map() -> dict:
    return json.loads((_REPO / "docs" / "perf" / "kpi-map.json").read_text())


def _happy_span() -> dict:
    return {
        "kind": "perf-span",
        "name": "open-to-library-rows",
        "duration_ms": 1800,
        "method": "navigationStart to first track row first paint",
        "stages": {"boot_source": 1},
    }


@pytest.mark.requirement("PERF-UI-03")
def test_happy_span_writes_numeric_open_to_library_rows_ms() -> None:
    """[if] a span is 1800 ms [then] one open_to_library_rows_ms row holds 1800.0, [else stop]."""
    rows = span_to_ledger_rows(
        _happy_span(),
        sha="abc123",
        machine="testhost",
        capture_date=_dt.date(2026, 9, 14),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["kpi"] == "open_to_library_rows_ms"
    assert row["value"] == 1800.0
    assert row["unit"] == "ms"
    assert row["capture_id"] == "perf-capture"


@pytest.mark.requirement("PERF-UI-03")
def test_missing_span_is_withheld() -> None:
    """[if] no span was captured [then] the ledger row is withheld with value null, [else stop]."""
    rows = span_to_ledger_rows(
        None,
        sha="abc123",
        machine="testhost",
        capture_date=_dt.date(2026, 9, 14),
        reason="missing span",
    )
    assert rows[0]["value"] is None
    assert rows[0]["status"] == "withheld"


@pytest.mark.requirement("PERF-UI-03")
def test_scorecard_reads_boot_lib() -> None:
    """[if] a happy span meets the shipped BOOT-LIB map [then] the verdict is PASS, [else stop]."""
    kpi_map = {"scenarios": {"BOOT-LIB": _shipped_map()["scenarios"]["BOOT-LIB"]}}
    entries = span_to_ledger_rows(
        _happy_span(),
        sha="abc123",
        machine="testhost",
        capture_date=_dt.date(2026, 9, 14),
    )
    (score,) = score_scenarios(kpi_map, entries, _dt.date(2026, 9, 14))
    assert score.scenario == "BOOT-LIB"
    assert score.verdict == "PASS"
