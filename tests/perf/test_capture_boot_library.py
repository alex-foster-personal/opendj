"""BOOT-LIB capture writer tests (issue #2697, PERF-UI-03)."""

from __future__ import annotations

import datetime as _dt
import json

import pytest

from scripts.perf import capture_boot_library
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


def test_playwright_wrapper_delegates_boot_library_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_runner(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(capture_boot_library, "run_playwright_capture", _fake_runner)

    result = capture_boot_library._run_playwright_capture(
        engine="http://engine.test",
        frontend="http://frontend.test",
        timeout_s=41,
    )

    assert result == {"ok": True}
    assert captured == {
        "engine": "http://engine.test",
        "frontend": "http://frontend.test",
        "frontend_root": capture_boot_library._FRONTEND_ROOT,
        "playwright_config": capture_boot_library._PLAYWRIGHT_CONFIG,
        "timeout_s": 41,
        "google_storage_state": None,
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
    assert row["round"] == "issue-2697"
    assert row["source"] == (
        "client perf-span open-to-library-rows from navigationStart to first track row paint"
    )
    assert row["method"] == "navigationStart to first track row first paint"
    assert list(row)[:11] == [
        "date",
        "round",
        "kpi",
        "value",
        "unit",
        "machine",
        "source",
        "method",
        "sha",
        "capture_id",
        "note",
    ]


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
