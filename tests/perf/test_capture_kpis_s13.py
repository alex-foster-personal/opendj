"""S13 login KPI browser capture writer and CLI tests."""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.perf.capture_kpis import main as capture_kpis_main
from scripts.perf.capture_ledger import span_to_ledger_rows
from scripts.perf.kpi_scorecard import UNKNOWN, score_scenarios

_REPO = Path(__file__).resolve().parents[2]


def _shipped_map() -> dict:
    return json.loads((_REPO / "docs" / "perf" / "kpi-map.json").read_text())


def _score_one(sid: str, entries: list[dict], today: _dt.date | None = None):
    kpi_map = {"scenarios": {sid: _shipped_map()["scenarios"][sid]}}
    today = today or _dt.date(2026, 9, 11)
    (score,) = score_scenarios(kpi_map, entries, today)
    return score


def _happy_span() -> dict:
    return {
        "kind": "perf-span",
        "name": "login-submit-to-library-usable",
        "duration_ms": 300,
        "method": "client-telemetry markLoginSubmit/markLoginNavigate to recordLibraryLoadTiming (in-app, Google excluded)",
        "stages": {
            "pre_navigate_ms": 100,
            "post_navigate_ms": 200,
            "full_wall_ms": 5000,
            "library_source": 1,
        },
    }


@pytest.mark.requirement("PERF-KPI-S13")
def test_happy_span_writes_two_numeric_rows() -> None:
    """[if] a happy S13 span is captured [then] it writes two numeric ledger rows, [else stop]."""
    rows = span_to_ledger_rows(
        _happy_span(),
        sha="abc123",
        machine="testhost",
        capture_date=_dt.date(2026, 9, 11),
    )
    assert len(rows) == 2
    scored = next(row for row in rows if row["kpi"] == "login_submit_to_library_usable_s")
    context = next(row for row in rows if row["kpi"] == "login_full_wall_s")
    assert scored["value"] == 0.3
    assert context["value"] == 5.0
    assert scored["unit"] == "s"
    assert context["unit"] == "s"
    assert scored["capture_id"] == "perf-capture"
    assert context["capture_id"] == "perf-capture"
    assert scored["sha"] == "abc123"
    assert scored["machine"] == "testhost"
    assert "Google consent excluded" in scored["source"]
    assert "Google excluded" in scored["method"]
    assert scored["value"] != 5.0


@pytest.mark.requirement("PERF-KPI-S13")
def test_missing_span_writes_withheld_rows() -> None:
    """[if] the S13 span is missing [then] it writes withheld rows with null values, [else stop]."""
    rows = span_to_ledger_rows(
        None,
        sha="abc123",
        machine="testhost",
        capture_date=_dt.date(2026, 9, 11),
        reason="missing library-usable mark",
    )
    assert len(rows) == 2
    for row in rows:
        assert row["value"] is None
        assert row["status"] == "withheld"
        assert row["measured"] is False
        assert "missing library-usable mark" in row["note"]
    encoded = json.dumps(rows)
    assert '"value": null' in encoded
    assert '"value": 0' not in encoded


@pytest.mark.requirement("PERF-KPI-S13")
def test_span_without_stages_is_withheld() -> None:
    """[if] an S13 span omits pre and post navigate stages [then] the rows are withheld, [else stop]."""
    span = {
        "kind": "perf-span",
        "name": "login-submit-to-library-usable",
        "duration_ms": 300,
        "stages": {"full_wall_ms": 5000},
    }
    rows = span_to_ledger_rows(
        span,
        sha="abc123",
        machine="testhost",
        capture_date=_dt.date(2026, 9, 11),
    )
    for row in rows:
        assert row["value"] is None
        assert row["status"] == "withheld"
        assert "cannot exclude Google" in row["note"]


@pytest.mark.requirement("PERF-KPI-S13")
def test_duration_mismatch_is_withheld() -> None:
    """[if] S13 duration_ms disagrees with stages [then] the rows are withheld, [else stop]."""
    span = _happy_span()
    span["duration_ms"] = 999
    rows = span_to_ledger_rows(
        span,
        sha="abc123",
        machine="testhost",
        capture_date=_dt.date(2026, 9, 11),
    )
    for row in rows:
        assert row["value"] is None
        assert row["status"] == "withheld"


@pytest.mark.requirement("PERF-KPI-S13")
def test_unreachable_engine_appends_withheld_rows(tmp_path: Path) -> None:
    """[if] the engine is unreachable [then] capture_kpis appends withheld S13 rows, [else stop]."""
    ledger = tmp_path / "kpi-ledger.json"
    ledger.write_text(
        json.dumps({"schema_version": 1, "entries": []}, indent=1) + "\n",
        encoding="utf-8",
    )
    exit_code = capture_kpis_main(
        [
            "--engine",
            "http://127.0.0.1:1",
            "--scenario",
            "S13",
            "--ledger",
            str(ledger),
        ]
    )
    assert exit_code != 0
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    rows = payload["entries"]
    assert len(rows) == 2
    for row in rows:
        assert row["value"] is None
        assert row["status"] == "withheld"
    assert all(
        row["kpi"] != "login_submit_to_library_usable_s" or row["value"] is None
        for row in rows
    )


@pytest.mark.requirement("PERF-KPI-S13")
def test_cli_help_names_s13() -> None:
    """[if] capture_kpis prints help [then] it names S13, [else stop]."""
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.perf.capture_kpis", "--help"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "S13" in proc.stdout


@pytest.mark.requirement("PERF-KPI-S13")
def test_scorecard_passes_on_happy_rows() -> None:
    """[if] the scorecard sees happy S13 rows [then] the verdict is PASS, [else stop]."""
    rows = span_to_ledger_rows(
        _happy_span(),
        sha="abc123",
        machine="testhost",
        capture_date=_dt.date(2026, 9, 11),
    )
    assert _score_one("S13", rows).verdict == "PASS"


@pytest.mark.requirement("PERF-KPI-S13")
def test_scorecard_unknown_on_withheld_rows() -> None:
    """[if] the scorecard sees withheld S13 rows [then] the verdict is UNKNOWN, [else stop]."""
    rows = span_to_ledger_rows(
        None,
        sha="abc123",
        machine="testhost",
        capture_date=_dt.date(2026, 9, 11),
        reason="missing library-usable mark",
    )
    score = _score_one("S13", rows)
    assert score.verdict == UNKNOWN
    rendered = str(score)
    assert "UNKNOWN" in rendered
    assert "login_submit_to_library_usable_s = " not in rendered
