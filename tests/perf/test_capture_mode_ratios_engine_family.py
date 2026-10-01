"""PERFMODE-15 round 8: the ratio capture counts PERFMODE-14's engine family.

`capture_mode_ratios` sampled only the Playwright Chromium tree, while
PERFMODE-15 says its cost is "measured the same way as PERFMODE-14", whose
family also holds the engine and every descendant of it (stem workers). These
tests use real processes for membership (psutil walks the real OS tree) and a
per-pid footprint reader so each pid's contribution is distinguishable:

- positive control: an engine whose cost differs between phases moves the ratio,
  while the browser-only diagnostic in the same note does not move;
- negative controls: a browser-only phase is refused as a ratio row, a process
  outside both trees is never read, and an engine root that exits mid-capture,
  overlaps the browser tree, or cannot be read raises instead of reading zero.
"""

from __future__ import annotations

import http.server
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import psutil
import pytest

from scripts.diagnostics.probe_native_metrics import DarwinProcessMetrics
from scripts.perf import capture_mode_ratios as cmr
from scripts.perf import mode_ratio_engine
from scripts.perf.capture_kpi_ledger import session_meta
from scripts.perf.mode_ratio_rows import PROCESS_FAMILY, gig_baseline_rows

_MB = 1024 * 1024
_IDS = [f"{deck}" * 40 for deck in "abcd"]

# A real process with one real child of its own, so the tree walk has a
# descendant to find: the launcher stands in for mode_ratio_browser.mjs (its
# child for Chromium), the engine for the engine (its child for a stem worker).
_PARENT_WITH_CHILD = (
    "import subprocess, sys, time\n"
    "subprocess.Popen(['sleep', '30'])\n"
    "print('ready', flush=True)\n"
    "time.sleep(30)\n"
)


@contextmanager
def _tree() -> Iterator[tuple[int, int]]:
    """A live (root pid, child pid) pair; both killed on exit."""
    root = subprocess.Popen([sys.executable, "-c", _PARENT_WITH_CHILD], stdout=subprocess.PIPE, text=True)
    try:
        assert root.stdout is not None and root.stdout.readline().strip() == "ready"
        children = psutil.Process(root.pid).children()
        assert len(children) == 1
        yield root.pid, children[0].pid
    finally:
        for child in psutil.Process(root.pid).children(recursive=True):
            child.kill()
        root.kill()
        root.wait(timeout=5)


class _PerPidNative:
    """phys_footprint by pid; records which pids were read. Unknown pids read 999 MB."""

    def __init__(self, footprint_mb_by_pid: dict[int, float], *, unreadable: int | None = None) -> None:
        self._by_pid = footprint_mb_by_pid
        self._unreadable = unreadable
        self.pids_read: list[int] = []

    def read(self, pid: int) -> SimpleNamespace:
        self.pids_read.append(pid)
        if pid == self._unreadable:
            raise psutil.AccessDenied(pid)
        return SimpleNamespace(phys_footprint=int(self._by_pid.get(pid, 999.0) * _MB))


def _sampler(launcher: int, native: _PerPidNative, engine_root: int | None) -> cmr._ProcessTreeSampler:
    return cmr._ProcessTreeSampler(
        launcher, engine_root_pid=engine_root, native=cast(DarwinProcessMetrics, native)
    )


# ----- sampler membership ----------------------------------------------------


@pytest.mark.requirement("PERFMODE-15")
def test_sample_counts_the_engine_and_its_descendant_and_nothing_else() -> None:
    """[if] an engine root is given [then] the total is browser plus engine tree and nothing outside them, [else stop]."""
    with _tree() as (launcher, browser_child), _tree() as (engine, engine_child):
        outsider = subprocess.Popen(["sleep", "30"])
        try:
            native = _PerPidNative(
                {launcher: 111.0, browser_child: 222.0, engine: 333.0, engine_child: 444.0, outsider.pid: 555.0}
            )
            reading = _sampler(launcher, native, engine).sample()
        finally:
            outsider.kill()
            outsider.wait(timeout=5)
    assert reading["browser_footprint_mb"] == pytest.approx(222.0)
    assert reading["engine_footprint_mb"] == pytest.approx(777.0)
    assert reading["physical_footprint_mb"] == pytest.approx(999.0)
    assert reading["engine_pid_count"] == 2.0
    assert set(native.pids_read) == {browser_child, engine, engine_child}


@pytest.mark.requirement("PERFMODE-15")
def test_sample_without_an_engine_root_stays_browser_only() -> None:
    """[if] no engine root is given (the leak capture) [then] only the browser tree is read, [else stop]."""
    with _tree() as (launcher, browser_child), _tree() as (engine, engine_child):
        native = _PerPidNative({browser_child: 222.0, engine: 333.0, engine_child: 444.0})
        reading = _sampler(launcher, native, None).sample()
    assert reading["physical_footprint_mb"] == pytest.approx(222.0)
    assert reading["engine_footprint_mb"] == 0.0
    assert set(native.pids_read) == {browser_child}


@pytest.mark.requirement("PERFMODE-15")
def test_sample_raises_when_the_engine_root_exits_mid_capture() -> None:
    """[if] the engine exits after the sampler pinned it [then] sample() raises, never reads zero, [else stop]."""
    with _tree() as (launcher, browser_child):
        engine = subprocess.Popen(["sleep", "30"])
        native = _PerPidNative({browser_child: 222.0, engine.pid: 333.0})
        sampler = _sampler(launcher, native, engine.pid)
        assert sampler.sample()["engine_footprint_mb"] == pytest.approx(333.0)
        engine.kill()
        engine.wait(timeout=5)
        with pytest.raises(RuntimeError, match="exited or was reused"):
            sampler.sample()


@pytest.mark.requirement("PERFMODE-15")
def test_sample_raises_when_the_engine_root_is_in_the_browser_tree() -> None:
    """[if] the engine root is also a browser descendant [then] sample() refuses double counting, [else stop]."""
    with _tree() as (launcher, browser_child):
        native = _PerPidNative({browser_child: 222.0})
        with pytest.raises(RuntimeError, match="both the browser and engine family"):
            _sampler(launcher, native, browser_child).sample()


@pytest.mark.requirement("PERFMODE-15")
def test_sample_raises_when_the_engine_root_cannot_be_read() -> None:
    """[if] the engine root's footprint is unreadable [then] sample() raises, not a zero engine, [else stop]."""
    with _tree() as (launcher, browser_child), _tree() as (engine, _engine_child):
        native = _PerPidNative({browser_child: 222.0}, unreadable=engine)
        with pytest.raises(psutil.AccessDenied):
            _sampler(launcher, native, engine).sample()


@pytest.mark.requirement("PERFMODE-15")
def test_sample_steady_carries_engine_fields_only_with_an_engine_root() -> None:
    """[if] _sample_steady runs with and without an engine root [then] engine fields appear only with one, [else stop]."""
    with _tree() as (launcher, browser_child), _tree() as (engine, engine_child):
        native = _PerPidNative({browser_child: 200.0, engine: 300.0, engine_child: 100.0})
        clock = iter(range(0, 10_000, cmr._PROBE_INTERVAL_S))
        with (
            patch("scripts.perf.capture_mode_ratios.DarwinProcessMetrics", return_value=native),
            patch("scripts.perf.capture_mode_ratios.time.sleep"),
            patch("scripts.perf.capture_mode_ratios.time.monotonic", new=lambda: float(next(clock))),
        ):
            with_engine = cmr._sample_steady(launcher, cmr._MIN_SAMPLE_S, engine)
            browser_only = cmr._sample_steady(launcher, cmr._MIN_SAMPLE_S, None)
    assert with_engine["footprint_mb"] == pytest.approx(600.0)
    assert with_engine["browser_footprint_mb"] == pytest.approx(200.0)
    assert with_engine["engine_footprint_mb"] == pytest.approx(400.0)
    assert with_engine["engine_pid_count_max"] == 2.0
    assert browser_only["footprint_mb"] == pytest.approx(200.0)
    assert not any(key.startswith("engine_") for key in browser_only)


# ----- ratio rows ------------------------------------------------------------


def _phase(browser_fp: float, browser_cpu: float, engine_fp: float, engine_cpu: float) -> dict[str, float]:
    return {
        "footprint_mb": browser_fp + engine_fp,
        "cpu_percent": browser_cpu + engine_cpu,
        "browser_footprint_mb": browser_fp,
        "browser_cpu_percent": browser_cpu,
        "engine_footprint_mb": engine_fp,
        "engine_cpu_percent": engine_cpu,
        "engine_pid_count_max": 1.0,
        "sample_count": 4.0,
    }


def _ratios(gig: dict[str, float], trackify: dict[str, float]) -> tuple[float, float, str]:
    rows = gig_baseline_rows(gig, trackify, list(_IDS), session_meta(sha="deadbeef"))
    assert all(row["process_family"] == PROCESS_FAMILY for row in rows)
    return rows[0]["value"], rows[1]["value"], str(rows[0]["note"])


def _browser_only_diagnostic(note: str) -> tuple[str, str]:
    footprint = re.search(r"browser_only_footprint_ratio=([\d.-]+)", note)
    cpu = re.search(r"browser_only_cpu_ratio=([\d.-]+)", note)
    assert footprint is not None and cpu is not None, note
    return footprint.group(1), cpu.group(1)


@pytest.mark.requirement("PERFMODE-15")
def test_engine_cost_moves_the_ratio_but_not_the_browser_only_diagnostic() -> None:
    """[if] only the engine's cost differs between two captures [then] the ratio moves and the browser-only diagnostic does not, [else stop]."""
    trackify = _phase(300.0, 20.0, 400.0, 5.0)
    light_engine = _ratios(_phase(1300.0, 100.0, 400.0, 5.0), trackify)
    heavy_engine = _ratios(_phase(1300.0, 100.0, 1400.0, 55.0), trackify)
    assert light_engine[0] == pytest.approx(1 - 700.0 / 1700.0, abs=1e-4)
    assert heavy_engine[0] == pytest.approx(1 - 700.0 / 2700.0, abs=1e-4)
    assert light_engine[1] == pytest.approx(1 - 25.0 / 105.0, abs=1e-4)
    assert heavy_engine[1] == pytest.approx(1 - 25.0 / 155.0, abs=1e-4)
    assert _browser_only_diagnostic(light_engine[2]) == ("0.7692", "0.8000")
    assert _browser_only_diagnostic(heavy_engine[2]) == ("0.7692", "0.8000")


@pytest.mark.requirement("PERFMODE-15")
def test_a_browser_only_phase_is_refused_as_a_ratio_row() -> None:
    """[if] a phase was sampled without the engine family [then] no ratio row is built, [else stop]."""
    browser_only = {"footprint_mb": 1300.0, "cpu_percent": 100.0, "sample_count": 4.0}
    with pytest.raises(SystemExit, match="no engine family"):
        gig_baseline_rows(browser_only, _phase(300.0, 20.0, 400.0, 5.0), list(_IDS), session_meta(sha="x"))


@pytest.mark.requirement("PERFMODE-15")
def test_a_zero_engine_phase_is_refused_as_a_ratio_row() -> None:
    """[if] a phase read a zero engine footprint [then] no ratio row is built, [else stop]."""
    with pytest.raises(SystemExit, match="zero engine is unmeasured"):
        gig_baseline_rows(
            _phase(1300.0, 100.0, 400.0, 5.0), _phase(300.0, 20.0, 0.0, 0.0), list(_IDS), session_meta(sha="x")
        )


# ----- the page reaches the sampled engine -----------------------------------


@contextmanager
def _build_info_server(pid: int) -> Iterator[str]:
    body = json.dumps({"pid": pid}).encode("utf-8")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status = 200 if self.path == "/api/v1/build-info" else 404
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.requirement("PERFMODE-15")
def test_frontend_must_reach_the_engine_being_sampled() -> None:
    """[if] the frontend's /api reaches a different engine pid [then] refused; the same pid passes, [else stop]."""
    with _build_info_server(4242) as frontend:
        assert mode_ratio_engine._frontend_engine_pid_reason(frontend, 4242) is None
        reason = mode_ratio_engine._frontend_engine_pid_reason(frontend, 4343)
    assert reason is not None and "does not use" in reason


# ----- PERFMODE-14's engineRootPids, reused through engine_root_pid.mjs ------

_ENGINE_STANDIN = r"""
import http.server, json, os, sys
body = json.dumps({"pid": os.getpid()}).encode()
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
server = http.server.HTTPServer(("127.0.0.1", 0), H)
print(server.server_address[1], flush=True)
server.serve_forever()
"""

_requires_darwin_lsof_node = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("lsof") is None or shutil.which("node") is None,
    reason="engineRootPids reads lsof and ps on the reference Mac (Darwin), driven through node",
)


@contextmanager
def _listener(argv0: str) -> Iterator[tuple[str, int]]:
    """A real loopback listener answering build-info with its own pid, run under `argv0`."""
    proc = subprocess.Popen(
        ["bash", "-c", 'exec -a "$0" "$1" -c "$2"', argv0, sys.executable, _ENGINE_STANDIN],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        port = int(proc.stdout.readline().strip())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(
            conn.laddr.port == port for conn in psutil.Process(proc.pid).net_connections("tcp")
        ):
            time.sleep(0.05)
        yield f"http://127.0.0.1:{port}", proc.pid
    finally:
        proc.kill()
        proc.wait(timeout=5)


@_requires_darwin_lsof_node
@pytest.mark.requirement("PERFMODE-15")
def test_engine_root_check_accepts_a_local_engine_listener() -> None:
    """[if] the listener on the engine port is a local process running engine code [then] it is the root, [else stop]."""
    with _listener("opendj-engine") as (origin, pid):
        assert mode_ratio_engine._engine_root_pid_reason(origin, pid) is None


@_requires_darwin_lsof_node
@pytest.mark.requirement("PERFMODE-15")
def test_engine_root_check_refuses_a_listener_that_is_not_the_engine() -> None:
    """[if] the listener's own command line does not run engine code [then] refused, [else stop]."""
    with _listener("some-port-forwarder") as (origin, pid):
        reason = mode_ratio_engine._engine_root_pid_reason(origin, pid)
    assert reason is not None and "does not name the engine" in reason


@_requires_darwin_lsof_node
@pytest.mark.requirement("PERFMODE-15")
def test_engine_root_check_refuses_a_changed_engine_pid() -> None:
    """[if] the engine answers with a pid other than the pinned one [then] refused, [else stop]."""
    with _listener("opendj-engine") as (origin, pid):
        reason = mode_ratio_engine._engine_root_pid_reason(origin, pid + 1)
    assert reason is not None and "engine pid changed" in reason
