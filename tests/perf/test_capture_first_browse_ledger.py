"""S9/S11 first-browse capture writer tests (issue #10240)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from scripts.perf import capture_first_browse
from scripts.perf.capture_first_browse_ledger import (
    S9_KPI,
    S9_METHOD,
    S11_KPI,
    S11_METHOD,
    first_browse_to_ledger_rows,
)
from scripts.perf.capture_kpi_ledger import CaptureMeta
from scripts.perf.kpi_scorecard import score_scenarios, verdict_for

_REPO = __import__("pathlib").Path(__file__).resolve().parents[2]

META = CaptureMeta(
    capture_id="perf-capture-20261110T000000Z",
    date="2026-11-10",
    machine="testhost",
    sha="abc123",
)


def _shipped_map() -> dict:
    return json.loads((_REPO / "docs" / "perf" / "kpi-map.json").read_text())


def _happy_first_browse() -> dict:
    return {
        "nav_start_to_click_ms": 4200,
        "rows_paint_to_click_ms": 900,
        "open_to_rows_ms": 3300,
        "clicks": 3,
        "rejected_attempts": 2,
    }


@pytest.mark.requirement("PERF-CAPTURE-11113")
def test_click_timings_become_s11_and_s9_rows() -> None:
    """[if] nav_start_to_click_ms is 4200 and rows_paint_to_click_ms is 900 [then] the S11 row holds 4.2 s and the S9 row 0.9 s, [else stop]."""
    rows = first_browse_to_ledger_rows(_happy_first_browse(), meta=META)
    assert len(rows) == 2
    s11, s9 = rows
    assert s11["kpi"] == S11_KPI
    assert s9["kpi"] == S9_KPI
    assert s11["value"] == 4.2
    assert s9["value"] == 0.9
    assert s11["unit"] == "s"
    assert s9["unit"] == "s"
    assert s11["method"] == S11_METHOD
    assert s9["method"] == S9_METHOD
    assert s11["measured"] is True
    assert s9["measured"] is True
    assert "status" not in s11
    assert "status" not in s9
    assert "excludes shell launch before navigationStart" in s11["note"]
    assert "rejected_attempts=2" in s11["note"]
    assert "open_to_rows_ms=3300" in s9["note"]
    assert list(s11)[:11] == [
        "date",
        "round",
        "kpi",
        "value",
        "unit",
        "machine",
        "sha",
        "source",
        "method",
        "capture_id",
        "note",
    ]


@pytest.mark.requirement("PERF-CAPTURE-11113")
def test_missing_click_withholds_both_rows_naming_the_timeout() -> None:
    """[if] no click is accepted [then] both rows are withheld naming the timeout, [else stop]."""
    reason = "timeout: no accepted row click within 90s (rejected_attempts=3)"
    rows = first_browse_to_ledger_rows(None, meta=META, reason=reason)
    assert len(rows) == 2
    for row in rows:
        assert row["value"] is None
        assert row["status"] == "withheld"
        assert row["measured"] is False
        assert "timeout" in row["note"]
        assert "rejected_attempts=3" in reason


@pytest.mark.requirement("PERF-CAPTURE-11113")
def test_missing_span_withholds_only_s9() -> None:
    """[if] rows_paint_to_click_ms is missing [then] S11 is measured and S9 withheld, [else stop]."""
    fb = {
        "nav_start_to_click_ms": 4200,
        "rows_paint_to_click_ms": None,
        "open_to_rows_ms": None,
        "clicks": 1,
        "rejected_attempts": 0,
        "span_reason": "missing open-to-library-rows perf-span",
    }
    rows = first_browse_to_ledger_rows(fb, meta=META)
    assert rows[0]["kpi"] == S11_KPI
    assert rows[0]["measured"] is True
    assert rows[1]["kpi"] == S9_KPI
    assert rows[1]["status"] == "withheld"
    assert "open-to-library-rows" in rows[1]["note"]


@pytest.mark.requirement("PERF-CAPTURE-11113")
def test_non_finite_or_bool_timings_are_withheld() -> None:
    """[if] a timing is non-finite or a bool [then] only that row is withheld, [else stop]."""
    nan_rows = first_browse_to_ledger_rows(
        {"nav_start_to_click_ms": float("nan"), "rows_paint_to_click_ms": 900},
        meta=META,
    )
    assert nan_rows[0]["status"] == "withheld"
    assert nan_rows[1]["measured"] is True

    neg_rows = first_browse_to_ledger_rows(
        {"nav_start_to_click_ms": 4200, "rows_paint_to_click_ms": -1},
        meta=META,
    )
    assert neg_rows[0]["measured"] is True
    assert neg_rows[1]["status"] == "withheld"

    bool_rows = first_browse_to_ledger_rows(
        {"nav_start_to_click_ms": True, "rows_paint_to_click_ms": 900},
        meta=META,
    )
    assert bool_rows[0]["status"] == "withheld"
    assert bool_rows[1]["measured"] is True


def test_playwright_wrapper_delegates_first_browse_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_runner(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(capture_first_browse, "run_playwright_capture", _fake_runner)

    result = capture_first_browse._run_playwright_capture(
        engine="http://engine.test",
        frontend="http://frontend.test",
        timeout_s=41,
    )

    assert result == {"ok": True}
    assert captured == {
        "engine": "http://engine.test",
        "frontend": "http://frontend.test",
        "frontend_root": capture_first_browse._FRONTEND_ROOT,
        "playwright_config": capture_first_browse._PLAYWRIGHT_CONFIG,
        "timeout_s": 41,
        "google_storage_state": None,
    }


def test_dry_run_prints_rows_and_appends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = tmp_path / "ledger.json"

    monkeypatch.setattr(capture_first_browse, "_probe_engine", lambda *_a, **_k: None)

    def _happy_capture(**_kwargs: object) -> dict[str, object]:
        return {"ok": True, "first_browse": _happy_first_browse(), "reason": None}

    monkeypatch.setattr(capture_first_browse, "_run_playwright_capture", _happy_capture)

    code = capture_first_browse.capture_first_browse(
        engine="http://127.0.0.1:8686",
        frontend="http://127.0.0.1:5273",
        ledger_path=ledger,
        dry_run=True,
    )
    assert code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert len(parsed) == 2
    assert not ledger.exists()


def test_probe_failure_withholds_both_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"schema_version": 2, "entries": []}, indent=2) + "\n")

    monkeypatch.setattr(
        capture_first_browse,
        "_probe_engine",
        lambda *_a, **_k: "engine unreachable: x",
    )

    code = capture_first_browse.capture_first_browse(
        engine="http://127.0.0.1:8686",
        frontend="http://127.0.0.1:5273",
        ledger_path=ledger,
        dry_run=False,
    )
    assert code == 1
    document = json.loads(ledger.read_text())
    assert len(document["entries"]) == 2
    assert all(entry.get("status") == "withheld" for entry in document["entries"])


@pytest.mark.requirement("PERF-CAPTURE-11113")
def test_measured_rows_score_against_shipped_map() -> None:
    """[if] S9 is 0.9 s and S11 is 4.2 s [then] both scenarios get a verdict, never UNMEASURED, [else stop]."""
    kpi_map = _shipped_map()
    entries = first_browse_to_ledger_rows(_happy_first_browse(), meta=META)
    scores = score_scenarios(kpi_map, entries, date(2026, 11, 10))
    by_id = {score.scenario: score for score in scores}
    assert by_id["S9"].verdict == verdict_for(0.9, kpi_map["scenarios"]["S9"])
    assert by_id["S11"].verdict == verdict_for(4.2, kpi_map["scenarios"]["S11"])
    assert by_id["S9"].verdict != "UNMEASURED"
    assert by_id["S11"].verdict != "UNMEASURED"
    assert by_id["S9"].verdict == "PASS"
    assert by_id["S11"].verdict == "ACCEPTABLE"
