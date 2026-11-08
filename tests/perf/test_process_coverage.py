"""Process coverage compares monitor RSS against an independent ps family."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import psutil
import pytest

from scripts.diagnostics.probe_process_family import ProcessRow
from scripts.diagnostics.probe_types import ProbeUnavailable
from scripts.perf import process_coverage
from scripts.perf.process_coverage import (
    JSON_KEYS,
    TELEMETRY_PATH,
    FamilySample,
    MeasurementError,
    fetch_telemetry_body,
    monitor_rss_mb,
)

FAMILY = [(100, "desktop-shell"), (101, "python-engine"), (102, "webkit-webcontent")]
SHELL_CMD = "/Applications/Open DJ.app/Contents/MacOS/opendj-desktop"
ENGINE_CMD = (
    "/Applications/Open DJ.app/Contents/Resources/payload/runtime/bin/python3 "
    "-m apps.engine_core serve"
)
WEBCONTENT_CMD = (
    "/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/"
    "com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent"
)
NOT_A_DAEMON = "/usr/libexec/some-other-daemon"


def _body(*live_mb: float) -> dict[str, object]:
    members: list[dict[str, object]] = [
        {
            "name": f"live-{index}",
            "source": "live",
            "role": "python-engine",
            "rss_mb": value,
        }
        for index, value in enumerate(live_mb)
    ]
    members.append(
        {
            "name": "WebContent",
            "source": "probe_log",
            "role": "webkit-webcontent",
            "physical_footprint_mb": 300.0,
        }
    )
    return {
        "available": True,
        "members": members,
        "totals": {"physical_footprint_mb": 9999.0},
    }


def _sample(
    rss: dict[int, float],
    cpu: dict[int, float],
    taken: float,
) -> FamilySample:
    return FamilySample(rss_mb_by_pid=rss, cpu_seconds_by_pid=cpu, taken_monotonic=taken)


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, object],
    samples_list: list[FamilySample],
) -> None:
    sample_iter = iter(samples_list)

    def _next_sample(_family: list[tuple[int, str]]) -> FamilySample:
        return next(sample_iter)

    monkeypatch.setattr(process_coverage, "fetch_telemetry_body", lambda port, *a, **k: body)
    monkeypatch.setattr(process_coverage, "discover_family", lambda shell_pid: (100, FAMILY))
    monkeypatch.setattr(process_coverage, "sample_family", _next_sample)


def test_496_of_936_prints_53_percent_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If broken: low monitor coverage does not fail the threshold gate."""
    body = _body(96.0, 400.0)
    rss = {100: 96.0, 101: 400.0, 102: 440.0}
    _patch(monkeypatch, body, [_sample(rss, {100: 0.0, 101: 0.0, 102: 0.0}, 0.0)])
    code = process_coverage.main(["--samples", "1"])
    out = capsys.readouterr().out
    assert code == 1
    assert "53.0 percent" in out


def test_940_covered_with_default_threshold_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If broken: coverage above 100 is clamped or the pass case regresses."""
    body = _body(96.0, 400.0, 444.0)
    rss = {100: 96.0, 101: 400.0, 102: 440.0}
    _patch(monkeypatch, body, [_sample(rss, {100: 0.0, 101: 0.0, 102: 0.0}, 0.0)])
    code = process_coverage.main(["--samples", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "100.4 percent" in out


def test_app_not_running_exits_2_and_prints_no_percentage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If broken: a closed port is misread as zero coverage instead of not running."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    def _never_discover(_shell_pid: int | None) -> tuple[int, list[tuple[int, str]]]:
        raise AssertionError("discover_family must not run when telemetry is unreachable")

    monkeypatch.setattr(process_coverage, "discover_family", _never_discover)
    code = process_coverage.main(["--port", str(port), "--samples", "1"])
    captured = capsys.readouterr()
    assert code == 2
    assert "Open DJ is not running" in captured.err
    assert captured.out == ""
    assert "percent" not in captured.out + captured.err


def test_app_not_running_in_json_mode_prints_nothing_on_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If broken: JSON mode leaks a percentage when the app is down."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setattr(
        process_coverage,
        "discover_family",
        lambda _shell_pid: (_ for _ in ()).throw(AssertionError("ps must not run")),
    )
    code = process_coverage.main(["--port", str(port), "--json", "--samples", "1"])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""


def test_json_emits_exactly_the_six_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If broken: machine output drifts from the KPI schema."""
    body = _body(96.0, 400.0)
    rss = {100: 96.0, 101: 400.0, 102: 440.0}
    _patch(monkeypatch, body, [_sample(rss, {100: 0.0, 101: 0.0, 102: 0.0}, 0.0)])
    code = process_coverage.main(["--json", "--samples", "1"])
    out = capsys.readouterr().out
    assert code == 1
    payload = json.loads(out)
    assert set(payload.keys()) == set(JSON_KEYS)
    assert payload["coverage_pct"] == 53.0
    assert payload["monitor_rss_mb"] == 496.0
    assert payload["family_rss_mb"] == 936.0
    assert payload["family_cpu_pct"] is None
    assert payload["samples"] == 1
    assert payload["by_role_mb"]["webkit-webcontent"] == 440.0


def test_fetch_reads_the_real_telemetry_route() -> None:
    """If broken: the script no longer targets the real telemetry path."""
    body = _body(96.0, 400.0)
    payload = json.dumps(body).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == TELEMETRY_PATH:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        fetched = fetch_telemetry_body(port)
        assert monitor_rss_mb(fetched) == 496.0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def test_http_404_is_unmeasurable_not_not_running() -> None:
    """If broken: HTTP 404 is treated as 'app not running' instead of unmeasurable."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(MeasurementError):
            fetch_telemetry_body(port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def test_unavailable_body_is_unmeasurable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If broken: available:false still prints a coverage percentage."""
    with pytest.raises(MeasurementError) as excinfo:
        monitor_rss_mb({"available": False, "reason": "probe not loaded"})
    assert "probe not loaded" in str(excinfo.value)

    body = {"available": False, "reason": "probe not loaded"}
    monkeypatch.setattr(process_coverage, "fetch_telemetry_body", lambda port, *a, **k: body)
    code = process_coverage.main(["--samples", "1"])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""


def test_webkit_helper_with_ppid_1_is_in_the_family(monkeypatch: pytest.MonkeyPatch) -> None:
    """If broken: WebContent with ppid 1 is dropped from the ps family."""
    rows = [
        ProcessRow(pid=100, ppid=1, pgid=100, command=SHELL_CMD),
        ProcessRow(pid=101, ppid=100, pgid=100, command=ENGINE_CMD),
        ProcessRow(pid=102, ppid=1, pgid=102, command=WEBCONTENT_CMD),
        ProcessRow(pid=50, ppid=1, pgid=50, command=NOT_A_DAEMON),
    ]
    starts = {100: 1000.0, 101: 1001.0, 102: 1005.0, 50: 10.0}

    class _FakeNative:
        def read(self, pid: int) -> SimpleNamespace:
            return SimpleNamespace(proc_start_abstime=starts[pid])

        def ticks_to_seconds(self, ticks: float) -> float:
            return float(ticks)

    monkeypatch.setattr(process_coverage, "process_table", lambda: rows)
    monkeypatch.setattr(process_coverage, "DarwinProcessMetrics", _FakeNative)

    shell_pid, family = process_coverage.discover_family(None)
    assert shell_pid == 100
    pids = {pid for pid, _role in family}
    assert 102 in pids
    assert 101 in pids
    assert 50 not in pids
    roles = dict(family)
    assert roles[102] == "webkit-webcontent"
    assert roles[101] == "python-engine"


def test_two_shells_without_pin_exit_2_naming_both(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If broken: two shells are silently merged without naming both pids."""
    rows = [
        ProcessRow(pid=100, ppid=1, pgid=100, command=SHELL_CMD),
        ProcessRow(pid=200, ppid=1, pgid=200, command=SHELL_CMD),
    ]

    class _FakeNative:
        def read(self, pid: int) -> SimpleNamespace:
            return SimpleNamespace(proc_start_abstime=0.0)

        def ticks_to_seconds(self, ticks: float) -> float:
            return float(ticks)

    monkeypatch.setattr(process_coverage, "fetch_telemetry_body", lambda port, *a, **k: _body(96.0, 400.0))
    monkeypatch.setattr(process_coverage, "process_table", lambda: rows)
    monkeypatch.setattr(process_coverage, "DarwinProcessMetrics", _FakeNative)
    code = process_coverage.main(["--samples", "1"])
    captured = capsys.readouterr()
    assert code == 2
    assert "100" in captured.err
    assert "200" in captured.err
    assert "--shell-pid" in captured.err
    assert captured.out == ""


def test_non_macos_is_unmeasurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """If broken: Linux hosts get a misleading coverage number instead of exit 2."""

    class _RaisesProbeUnavailable:
        def __init__(self) -> None:
            raise ProbeUnavailable("darwin only")

    monkeypatch.setattr(process_coverage, "DarwinProcessMetrics", _RaisesProbeUnavailable)
    with pytest.raises(MeasurementError) as excinfo:
        process_coverage.discover_family(None)
    assert "macOS" in str(excinfo.value)


def test_family_cpu_pct_two_samples() -> None:
    """If broken: CPU percent ignores the wall interval."""
    s0 = _sample({100: 1.0, 101: 5.0}, {100: 1.0, 101: 5.0}, 0.0)
    s1 = _sample({100: 1.0, 101: 5.0}, {100: 2.0, 101: 6.0}, 10.0)
    assert process_coverage.family_cpu_pct([s0, s1]) == 20.0


def test_family_cpu_pct_ignores_pid_only_in_second_sample() -> None:
    """If broken: a new pid in the second sample inflates CPU."""
    s0 = _sample({100: 1.0, 101: 5.0}, {100: 1.0, 101: 5.0}, 0.0)
    s1 = _sample(
        {100: 1.0, 101: 5.0, 102: 0.0},
        {100: 2.0, 101: 6.0, 102: 50.0},
        10.0,
    )
    assert process_coverage.family_cpu_pct([s0, s1]) == 20.0


def test_family_cpu_pct_means_multiple_intervals() -> None:
    """If broken: three samples use only the first interval."""
    s0 = _sample({100: 0.0}, {100: 0.0}, 0.0)
    s1 = _sample({100: 0.0}, {100: 1.0}, 10.0)
    s2 = _sample({100: 0.0}, {100: 3.0}, 20.0)
    assert process_coverage.family_cpu_pct([s0, s1, s2]) == 15.0


def test_family_cpu_pct_none_for_short_series() -> None:
    """If broken: one sample reports a CPU percent."""
    assert process_coverage.family_cpu_pct([]) is None
    s0 = _sample({100: 1.0}, {100: 1.0}, 0.0)
    assert process_coverage.family_cpu_pct([s0]) is None


def test_collect_samples_sleeps_only_between_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    """If broken: samples=1 still sleeps or samples=3 sleeps the wrong count."""
    sleeps: list[float] = []
    monkeypatch.setattr(process_coverage, "sample_family", lambda _f: _sample({}, {}, 0.0))

    process_coverage.collect_samples(FAMILY, 1, 5.0, sleep=sleeps.append)
    assert sleeps == []
    sleeps.clear()
    process_coverage.collect_samples(FAMILY, 3, 5.0, sleep=sleeps.append)
    assert sleeps == [5.0, 5.0]


def test_sample_family_skips_a_vanished_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """If broken: NoSuchProcess aborts the whole sample."""

    class _FakeProc:
        def oneshot(self) -> _FakeProc:
            return self

        def __enter__(self) -> _FakeProc:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def memory_info(self) -> SimpleNamespace:
            return SimpleNamespace(rss=104857600)

        def cpu_times(self) -> SimpleNamespace:
            return SimpleNamespace(user=1.0, system=0.5)

    def _factory(pid: int) -> _FakeProc:
        if pid == 999:
            raise psutil.NoSuchProcess(pid)
        return _FakeProc()

    monkeypatch.setattr(process_coverage.psutil, "Process", _factory)
    sample = process_coverage.sample_family([(100, "desktop-shell"), (999, "python-engine")])
    assert 999 not in sample.rss_mb_by_pid
    assert sample.rss_mb_by_pid[100] == 100.0
    assert sample.cpu_seconds_by_pid[100] == 1.5


def test_samples_zero_is_rejected() -> None:
    """If broken: zero samples silently succeeds."""
    with pytest.raises(SystemExit) as excinfo:
        process_coverage.main(["--samples", "0"])
    assert excinfo.value.code == 2


def test_multi_sample_text_reports_cpu(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If broken: multi-sample runs omit the CPU line in text mode."""
    body = _body(96.0, 400.0, 444.0)
    rss = {100: 96.0, 101: 400.0, 102: 440.0}
    two = [
        _sample(rss, {100: 0.0, 101: 0.0, 102: 0.0}, 0.0),
        _sample(rss, {100: 1.0, 101: 0.0, 102: 0.0}, 10.0),
    ]
    monkeypatch.setattr(process_coverage, "fetch_telemetry_body", lambda port, *a, **k: body)
    monkeypatch.setattr(process_coverage, "discover_family", lambda shell_pid: (100, FAMILY))
    monkeypatch.setattr(
        process_coverage,
        "collect_samples",
        lambda family, n, interval, sleep=process_coverage.time.sleep: two,
    )
    code = process_coverage.main(["--samples", "2", "--interval", "0"])
    out = capsys.readouterr().out
    assert code == 0
    assert "family CPU: 10.0 percent" in out
