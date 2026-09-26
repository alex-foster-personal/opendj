"""S13 login KPI browser capture writer and CLI tests."""

from __future__ import annotations

import datetime as _dt
import json
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from scripts.perf import capture_s13
from scripts.perf.capture_kpis import main as capture_kpis_main
from scripts.perf.capture_ledger import (
    append_ledger_rows,
    classify_s13_withhold_reason,
    span_to_ledger_rows,
)
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


# REQ: PERF-CAPTURE-03
@pytest.mark.requirement("PERF-KPI-S13")
def test_classify_s13_withhold_reason_restored_session() -> None:
    """[if] submit marks are absent [then] reason is restored-session, [else stop]."""
    reason = classify_s13_withhold_reason(
        "restored-session: submit or navigate mark missing after login"
    )
    assert reason.startswith("restored-session:")


# REQ: PERF-CAPTURE-03
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

@contextmanager
def _hanging_http_server() -> Iterator[str]:
    """A REAL listening socket that accepts and never answers.

    Not a mock: the connection completes, the request is sent, and the client
    blocks on the response until its own timeout fires -- which is the exact
    transport failure a wedged engine produces. A mocked `urlopen` proves only
    that the except arm is reachable, not that the real socket path reaches it.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    held: list[socket.socket] = []
    stop = threading.Event()

    def _accept_and_hold() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
            except (TimeoutError, OSError):
                continue
            held.append(conn)

    thread = threading.Thread(target=_accept_and_hold, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}"
    finally:
        stop.set()
        thread.join(timeout=2)
        for conn in held:
            conn.close()
        server.close()


def test_http_json_turns_a_real_socket_timeout_into_connection_error() -> None:
    """[if] the engine accepts but never answers [then] _http_json raises
    ConnectionError naming the timeout, [else the caller cannot withhold]."""
    with _hanging_http_server() as base, pytest.raises(ConnectionError) as excinfo:
        capture_s13._http_json("GET", f"{base}/api/v1/health", timeout_s=0.5)
    assert "TimeoutError" in str(excinfo.value)


# REQ: PERF-CAPTURE-03
def test_engine_socket_timeout_withholds_instead_of_crashing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A slow engine must withhold, never raise.

    Measured Sat 12 Sep 2026 on a host at load average 118: the probe's
    urlopen raised `TimeoutError`, which is an OSError and not a URLError, so
    it escaped `_http_json` and the capture died with a traceback instead of
    writing the withheld row S13 promises. Driven here through a real socket
    that accepts and stays silent.
    """
    ledger = tmp_path / "kpi-ledger.json"
    ledger.write_text(
        json.dumps({"schema_version": 1, "entries": []}, indent=1) + "\n",
        encoding="utf-8",
    )
    with _hanging_http_server() as base:
        code = capture_s13.capture_s13(
            engine=base,
            frontend=base,
            ledger_path=ledger,
            probe_timeout_s=0.5,
        )

    assert code == 1
    rows = json.loads(ledger.read_text(encoding="utf-8"))["entries"]
    scored = [r for r in rows if r["kpi"] == "login_submit_to_library_usable_s"]
    assert len(scored) == 1
    assert scored[0]["value"] is None
    assert scored[0]["status"] == "withheld"
    assert "TimeoutError" in scored[0]["note"]
    assert "engine unreachable" in capsys.readouterr().err


def _git_numstat(orig: Path, updated: Path) -> tuple[int, int]:
    result = subprocess.run(
        ["git", "diff", "--numstat", "--no-index", str(orig), str(updated)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, "append must produce a non-empty git diff"
    parts = result.stdout.strip().split()
    assert len(parts) >= 2
    return int(parts[0]), int(parts[1])


def _historical_s13_row() -> dict:
    return {
        "date": "2026-09-01",
        "round": "issue-1885",
        "kpi": "login_submit_to_library_usable_s",
        "value": 0.5,
        "unit": "s",
        "machine": "testhost",
        "source": (
            "client-telemetry login span (submit mark to first recordLibraryLoadTiming); "
            "scored value is in-app (Google consent excluded)"
        ),
        "method": (
            "client-telemetry markLoginSubmit/markLoginNavigate to recordLibraryLoadTiming "
            "(in-app, Google excluded)"
        ),
        "sha": "oldsha",
        "capture_id": "perf-capture",
        "note": "historical row",
    }


@pytest.mark.parametrize("indent_width", [2, 4])
@pytest.mark.requirement("PERF-KPI-S13")
def test_append_ledger_rows_preserves_fixture_indent(
    tmp_path: Path, indent_width: int
) -> None:
    """[if] append_ledger_rows appends rows [then] git diff shows no deleted lines, [else stop]."""
    historical = _historical_s13_row()
    ledger = tmp_path / "kpi-ledger.json"
    orig_payload = {"schema_version": 2, "entries": [historical]}
    orig_text = json.dumps(orig_payload, indent=indent_width, ensure_ascii=False) + "\n"
    ledger.write_text(orig_text, encoding="utf-8")
    orig_copy = tmp_path / "orig.json"
    orig_copy.write_text(orig_text, encoding="utf-8")

    new_rows = span_to_ledger_rows(
        _happy_span(),
        sha="newsha",
        machine="testhost",
        capture_date=_dt.date(2026, 9, 11),
    )
    append_ledger_rows(ledger, new_rows)

    updated_text = ledger.read_text(encoding="utf-8")
    updated_payload = json.loads(updated_text)
    assert updated_payload["entries"][0] == historical
    assert updated_payload["entries"][1:] == new_rows
    expected = json.dumps(updated_payload, indent=indent_width, ensure_ascii=False) + "\n"
    assert updated_text == expected

    added, deleted = _git_numstat(orig_copy, ledger)
    assert added > 0
    assert deleted == 0


@pytest.mark.requirement("PERF-KPI-S13")
def test_append_ledger_rows_rejects_unindented_ledger(tmp_path: Path) -> None:
    """[if] the ledger is not pretty-printed [then] append fails, never rewrites it, [else stop]."""
    ledger = tmp_path / "kpi-ledger.json"
    ledger.write_text('{"schema_version":1,"entries":[]}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="cannot infer JSON indent"):
        append_ledger_rows(ledger, [_historical_s13_row()])


def test_a_reachable_engine_is_not_reported_as_a_timeout() -> None:
    """Negative control: the hanging-socket fixture must not make every probe
    look timed out. A CLOSED port is refused promptly and names refusal, not a
    timeout, so the assertion above is discriminating rather than universal."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    with pytest.raises(ConnectionError) as excinfo:
        capture_s13._http_json(
            "GET", f"http://127.0.0.1:{closed_port}/api/v1/health", timeout_s=2.0
        )
    assert "TimeoutError" not in str(excinfo.value)
