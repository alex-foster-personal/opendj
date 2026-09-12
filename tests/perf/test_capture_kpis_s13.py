"""S13 login KPI browser capture writer and CLI tests."""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from scripts.perf import capture_s13
from scripts.perf.capture_kpis import main as capture_kpis_main
from scripts.perf.capture_ledger import classify_s13_withhold_reason, span_to_ledger_rows
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


@pytest.mark.requirement("PERF-KPI-S13")
def test_classify_s13_withhold_reason_restored_session() -> None:
    """[if] submit marks are absent [then] reason is restored-session, [else stop]."""
    reason = classify_s13_withhold_reason(
        "restored-session: submit or navigate mark missing after login"
    )
    assert reason.startswith("restored-session:")


@pytest.mark.requirement("PERF-KPI-S13")
def test_classify_s13_withhold_reason_missing_telemetry() -> None:
    """[if] marks exist but no span POST [then] the reason is missing-telemetry, [else stop]."""
    reason = classify_s13_withhold_reason("missing library-usable mark: no perf-span POST")
    assert reason.startswith("missing-telemetry:")


@pytest.mark.requirement("PERF-KPI-S13")
def test_withheld_capture_prints_stderr_and_names_ledger_row(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] S13 capture is withheld [then] stderr names the note and ledger row, [else stop]."""
    ledger = tmp_path / "kpi-ledger.json"
    ledger.write_text(
        json.dumps({"schema_version": 1, "entries": []}, indent=1) + "\n",
        encoding="utf-8",
    )

    def _fake_playwright(**_kwargs: object) -> dict[str, object]:
        return {
            "ok": False,
            "span": None,
            "reason": (
                "missing-telemetry: login marks present but no perf-span POST "
                "(library-usable hooks absent or library did not reach first paint)"
            ),
        }

    monkeypatch.setattr(capture_s13, "_probe_engine", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(capture_s13, "_run_playwright_capture", _fake_playwright)

    exit_code = capture_s13.capture_s13(
        engine="http://127.0.0.1:8686",
        frontend="http://127.0.0.1:8686",
        ledger_path=ledger,
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "missing-telemetry:" in captured.err
    assert "login_submit_to_library_usable_s" in captured.err
    assert "WITHHELD" in captured.err
    assert str(ledger) in captured.err
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["entries"][0]["kpi"] == "login_submit_to_library_usable_s"
    assert payload["entries"][0]["status"] == "withheld"


@pytest.mark.requirement("PERF-KPI-S13")
def test_unreachable_engine_prints_withheld_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] engine probe fails [then] stderr names the withheld ledger row, [else stop]."""
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
    captured = capsys.readouterr()

    assert exit_code != 0
    assert "login_submit_to_library_usable_s" in captured.err
    assert "WITHHELD" in captured.err
    assert str(ledger) in captured.err


def test_engine_socket_timeout_withholds_instead_of_crashing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A slow engine must withhold, never raise.

    Measured Sat 12 Sep 2026 on a host at load average 118: the probe's
    `urlopen(timeout=10)` raised `TimeoutError`, which is an OSError and not a
    URLError, so it escaped `_http_json` and the capture died with a traceback
    instead of writing the withheld row S13 promises.
    """
    ledger = tmp_path / "kpi-ledger.json"
    ledger.write_text(
        json.dumps({"schema_version": 1, "entries": []}, indent=1) + "\n",
        encoding="utf-8",
    )

    def _raise_timeout(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError("timed out")

    with mock.patch.object(capture_s13, "urlopen", _raise_timeout):
        code = capture_s13.capture_s13(
            engine="http://127.0.0.1:9",
            frontend="http://127.0.0.1:9",
            ledger_path=ledger,
        )

    assert code == 1
    rows = json.loads(ledger.read_text(encoding="utf-8"))["entries"]
    scored = [r for r in rows if r["kpi"] == "login_submit_to_library_usable_s"]
    assert len(scored) == 1
    assert scored[0]["value"] is None
    assert scored[0]["status"] == "withheld"
    assert "TimeoutError" in scored[0]["note"]
    assert "engine unreachable" in capsys.readouterr().err


def test_http_json_turns_a_socket_timeout_into_connection_error() -> None:
    """The negative control for the guard above: without the OSError arm this
    call raises TimeoutError, which no caller catches."""
    with mock.patch.object(capture_s13, "urlopen", side_effect=TimeoutError("timed out")):
        with pytest.raises(ConnectionError) as excinfo:
            capture_s13._http_json("GET", "http://127.0.0.1:9/api/v1/health")
    assert "TimeoutError" in str(excinfo.value)
