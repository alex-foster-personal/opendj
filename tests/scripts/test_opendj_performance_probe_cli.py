"""CLI contract coverage for the OpenDJ performance probe's trend command.

Separate from the aggregation lane because everything here drives `main` or the
engine's client-error sink over a real socket, rather than calling an
aggregation function directly.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from scripts.diagnostics.opendj_performance_probe import (
    TREND_REPORT_FAILURE_EXIT,
    OpenDJProbe,
    ProcessRow,
    TrendReportError,
    main,
    report_red_trend,
)
from tests.scripts.probe_report_servers import serving_engine as _serving_engine
from tests.scripts.probe_report_servers import stop_engine as _stop_engine
from tests.scripts.probe_sample_fixtures import captured_record as _captured_record
from tests.scripts.probe_sample_fixtures import write_trend_rows as _write_trend_rows


def _closed_local_port() -> int:
    """A real port with nothing listening, so a connection is really refused."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _ClientErrorSink(BaseHTTPRequestHandler):
    """A real HTTP endpoint standing in for the engine's client-error route."""

    status = 202

    def do_GET(self) -> None:
        if self.path != "/api/v1/health":
            self.send_error(404)
            return
        body = b'{"status":"ok","version":"test"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(type(self).status)
        body = b'{"event_id":"sink-event","stored":true}'
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


def _serving_sink(status: int) -> tuple[HTTPServer, int]:
    handler = type("_Sink", (_ClientErrorSink,), {"status": status})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


class _MalformedReceiptSink(_ClientErrorSink):
    """A 202-accepting sink whose receipt body is not valid JSON.

    Reproduces a real engine response mid-write or truncated by a proxy: the
    HTTP contract (202) is honored but the body cannot be decoded.
    """

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(202)
        body = b"not-json"
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serving_malformed_sink() -> tuple[HTTPServer, int]:
    server = HTTPServer(("127.0.0.1", 0), _MalformedReceiptSink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


def test_report_red_trend_raises_when_the_connection_is_refused() -> None:
    """A RED trend nobody could record must not read as a quiet False."""

    port = _closed_local_port()

    with pytest.raises(TrendReportError) as raised:
        report_red_trend(port, {"verdict_reason": "unload retained 400.0 MB above baseline"})

    message = str(raised.value)
    assert f"127.0.0.1:{port}" in message
    assert "/health" in message
    assert "ConnectionRefusedError" in message or "URLError" in message


def test_report_red_trend_raises_when_the_engine_rejects_the_post() -> None:
    """A reachable engine that answers 500 has still not recorded anything."""

    server, port = _serving_sink(500)
    try:
        with pytest.raises(TrendReportError) as raised:
            report_red_trend(port, {"verdict_reason": "1 suspected orphan(s)"})
    finally:
        server.shutdown()

    assert "HTTP Error 500" in str(raised.value)


def test_report_red_trend_raises_on_an_unexpected_success_status() -> None:
    """A 200 is not the 202 this sink promises, so nothing was durably queued.

    urllib raises HTTPError for 5xx on its own, so that path never reaches the
    status comparison. A non-error, non-202 status is the case that does, and
    it is the one the old `return response.status == 202` turned into a quiet
    False.
    """

    server, port = _serving_sink(200)
    try:
        with pytest.raises(TrendReportError) as raised:
            report_red_trend(port, {"verdict_reason": "1 suspected orphan(s)"})
    finally:
        server.shutdown()

    assert "HTTP 200, expected 202" in str(raised.value)


def test_report_red_trend_raises_when_the_receipt_body_is_not_json() -> None:
    """A 202 with an undecodable body must still surface as a TrendReportError.

    `json.load` raises `json.JSONDecodeError`, a `ValueError` subclass, which
    is a different exception family from the `OSError`/`URLError` the prior
    tests exercise. Left uncaught, it would escape `report_red_trend` as a
    raw exception instead of the `TrendReportError` every caller expects,
    silently skipping both the RED verdict and the `client_error_posted`
    error the exit-3 contract requires.
    """

    server, port = _serving_malformed_sink()
    try:
        with pytest.raises(TrendReportError) as raised:
            report_red_trend(port, {"verdict_reason": "1 suspected orphan(s)"})
    finally:
        server.shutdown()

    assert "JSONDecodeError" in str(raised.value)


def test_report_red_trend_persists_through_the_production_engine_route(tmp_path: Path) -> None:
    """A RED report uses the production route and leaves its durable record.

    The prior fake HTTP sink accepted every payload and discarded it. This
    calls the real FastAPI client-error route through a loopback socket and
    checks the exact record persisted by the engine.
    """

    server, thread, port = _serving_engine(tmp_path)
    try:
        receipt = report_red_trend(port, {"verdict_reason": "1 suspected orphan(s)"})
    finally:
        _stop_engine(server, thread)

    assert receipt["posted"] is True
    assert receipt["http_status"] == 202
    assert receipt["client_event_id"].startswith("performance-trend-")
    records = list(tmp_path.glob("webui-client-errors-*.log"))
    assert len(records) == 1
    persisted = json.loads(records[0].read_text(encoding="utf-8"))
    assert persisted["client_event_id"] == receipt["client_event_id"]
    assert persisted["name"] == "OpenDJPerformanceTrend"


def _red_rows_with_engine_port(port: int) -> list[dict[str, object]]:
    rows = [
        _captured_record("2026-08-21T20:00:00Z", 300.0, 100.0),
        _captured_record("2026-08-21T20:15:00Z", 900.0, 700.0),
        _captured_record("2026-08-21T20:30:00Z", 700.0, 500.0),
    ]
    for row, decks in zip(rows, (0, 4, 0), strict=True):
        row["browser_perf_ring"] = {"available": True, "loaded_deck_count": decks}
    rows[-1]["engine"] = {"jobs": {"active": []}, "port": port}
    return rows


def test_trend_exits_distinctly_when_the_red_report_cannot_be_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RED plus an unwritable record is its own exit code, not a silent 1."""

    port = _closed_local_port()
    _write_trend_rows(tmp_path, _red_rows_with_engine_port(port))

    code = main(["trend", "--since", "2026-08-21T20:00:00Z", "--output-dir", str(tmp_path)])

    printed = json.loads(capsys.readouterr().out)
    assert code == TREND_REPORT_FAILURE_EXIT
    assert printed["verdict"] == "RED"
    assert printed["client_error_posted"]["posted"] is False
    assert f"127.0.0.1:{port}" in printed["client_error_posted"]["error"]


def test_trend_exits_nonzero_when_a_selected_log_window_is_malformed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corrupt record must not be silently omitted from a GREEN trend."""

    _write_trend_rows(tmp_path, [_captured_record("2026-08-21T20:00:00Z", 300.0, 100.0)])
    path = tmp_path / "opendj-performance-2026-08-21.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{not json}\n", encoding="utf-8")

    code = main(["trend", "--since", "2026-08-21T20:00:00Z", "--output-dir", str(tmp_path)])

    printed = json.loads(capsys.readouterr().out)
    assert code != 0
    assert printed["available"] is False
    assert "malformed" in printed["reason"]


def test_pid_zero_is_rejected_before_sampling(tmp_path: Path) -> None:
    """PID zero is never an alias for automatic OpenDJ shell detection."""

    with pytest.raises(SystemExit) as raised:
        main(["--once", "--pid", "0", "--output-dir", str(tmp_path)])

    assert "--pid must be positive" in str(raised.value.code)


def test_match_requires_one_process_instead_of_selecting_table_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renderer selector with two candidates must name their PIDs and fail."""

    rows = [
        ProcessRow(pid=101, ppid=1, pgid=101, command="Chrome Renderer :9448"),
        ProcessRow(pid=202, ppid=1, pgid=202, command="Chrome Renderer :9448"),
    ]
    monkeypatch.setattr("scripts.diagnostics.opendj_performance_probe.process_table", lambda: rows)
    monkeypatch.setattr(
        "scripts.diagnostics.opendj_performance_probe.DarwinProcessMetrics", lambda: None
    )
    probe = OpenDJProbe(None, (), match="Chrome Renderer")

    with pytest.raises(Exception, match="candidate PIDs: 101, 202"):
        probe.sample(deep=False)


def test_trend_exits_distinctly_when_no_engine_port_was_ever_sampled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RED with no engine port in any sample is a report failure, not a bare 1.

    Before the fix the port check sat on the same condition as the verdict, so
    a RED window without a port skipped reporting entirely and exited 1 with
    no `client_error_posted` at all: the missing durable record was invisible.
    """

    rows = _red_rows_with_engine_port(1)
    for row in rows:
        row.pop("engine", None)
    _write_trend_rows(tmp_path, rows)

    code = main(["trend", "--since", "2026-08-21T20:00:00Z", "--output-dir", str(tmp_path)])

    printed = json.loads(capsys.readouterr().out)
    assert code == TREND_REPORT_FAILURE_EXIT
    assert printed["verdict"] == "RED"
    assert printed["client_error_posted"]["posted"] is False
    assert "engine port" in printed["client_error_posted"]["error"]


def test_trend_exits_one_when_the_red_report_is_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Control: the same RED window with a live sink keeps the RED exit of 1.

    This is what stops the test above from passing because every trend now
    exits 3.
    """

    server, port = _serving_sink(202)
    _write_trend_rows(tmp_path, _red_rows_with_engine_port(port))
    try:
        code = main(["trend", "--since", "2026-08-21T20:00:00Z", "--output-dir", str(tmp_path)])
    finally:
        server.shutdown()

    printed = json.loads(capsys.readouterr().out)
    assert code == 1
    assert printed["verdict"] == "RED"
    assert printed["client_error_posted"]["posted"] is True


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        (["--pid", "4321"], "--pid"),
        (["--match", "OpenDJ"], "--match"),
        (["--shell-pid", "712"], "--shell-pid"),
        (["--bundle-id", "com.opendj.desktop"], "--bundle-id"),
    ],
)
def test_trend_rejects_a_sampling_selector_it_cannot_apply(
    tmp_path: Path, selector: list[str], expected: str
) -> None:
    """An accepted-but-ignored selector computed a verdict over other processes."""

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "trend",
                "--since",
                "2026-08-21T20:00:00Z",
                "--output-dir",
                str(tmp_path),
                *selector,
            ]
        )

    assert expected in str(raised.value.code)
    assert "not applied by trend" in str(raised.value.code)


def test_trend_rejects_the_summary_flag_instead_of_silently_summarizing(
    tmp_path: Path,
) -> None:
    """`trend --summary --since X` used to print a summary and exit 0.

    That took the earlier --summary branch, so a RED trend never ran and its
    nonzero-exit contract was skipped in the one mode that most looks like it
    is checking the trend.
    """

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "trend",
                "--summary",
                "--since",
                "2026-08-21T20:00:00Z",
                "--output-dir",
                str(tmp_path),
            ]
        )

    assert "trend and --summary are mutually exclusive" in str(raised.value.code)


def test_since_is_rejected_outside_trend_mode(tmp_path: Path) -> None:
    """--since is only read by trend, so accepting it elsewhere ignored it."""

    with pytest.raises(SystemExit) as raised:
        main(["--summary", "--since", "2026-08-21T20:00:00Z", "--output-dir", str(tmp_path)])

    assert "--since only applies to the trend command" in str(raised.value.code)


def test_trend_still_runs_with_only_the_arguments_it_supports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Control: the rejections above did not make plain trend unrunnable."""

    _write_trend_rows(
        tmp_path,
        [
            _captured_record("2026-08-21T20:00:00Z", 1600.0, 1300.0),
            _captured_record("2026-08-21T20:30:00Z", 1600.0, 1300.0),
        ],
    )

    code = main(["trend", "--since", "2026-08-21T20:00:00Z", "--output-dir", str(tmp_path)])

    printed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert printed["verdict"] == "GREEN"
