"""Capture Trackify vs Gig steady-state ratios and 1h leak slope (PERFMODE-15).

macOS reference capture only. Refuses Linux hosts with an explicit message.

Usage (reference Mac, engine + frontend already running):
  uv run python -m scripts.perf.capture_mode_ratios --mode trackify --gig-baseline \\
    --frontend http://127.0.0.1:5273 --duration-s 60 --ledger docs/perf/kpi-ledger.json
  uv run python -m scripts.perf.capture_mode_ratios --mode trackify --leak-duration-s 3600 \\
    --frontend http://127.0.0.1:5273 --ledger docs/perf/kpi-ledger.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import IO, Any

import psutil

from scripts.diagnostics.probe_log_store import _linear_slope_mb_per_hour
from scripts.perf.capture_kpi_ledger import build_row, session_meta
from scripts.perf.capture_ledger import append_ledger_rows

_REPO = Path(__file__).resolve().parents[2]
_FRONTEND_ROOT = _REPO / "apps" / "webui" / "frontend"
_BROWSER_SCRIPT = _REPO / "scripts" / "perf" / "mode_ratio_browser.mjs"
_MIN_SAMPLE_S = 60
_PROBE_INTERVAL_S = 15
_BROWSER_SERVICE_ID = "com.af.music-dj-tools.mode-ratio-browser"


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise SystemExit(
            "capture_mode_ratios refuses non-macOS capture: run on the reference Mac "
            "with opendj_performance_probe installed"
        )


def _git_sha() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


class _ProcessTreeSampler:
    """Samples footprint and CPU of a live process and its descendants.

    `mode_ratio_browser.mjs` drives Gig and Trackify in a Playwright-launched
    Chromium against the dev frontend -- NOT the packaged desktop app. The
    engine's `/api/v1/performance/telemetry/processes` endpoint reports the
    packaged app's own process family (desktop-shell / python-engine /
    webkit-webcontent) read from a native diagnostics log, which has nothing
    to do with this Chromium instance: sampling it would compute a
    footprint/CPU ratio over processes whose cost barely moves between
    modes, then publish that as "Trackify savings vs Gig" (claude-review
    finding on PR #3676). This sampler instead walks the actual OS process
    tree rooted at the browser subprocess's PID (Chromium's main process
    plus every renderer/GPU helper it spawns), which is where the
    AudioContexts, decoded PCM and waveform work this KPI is about actually
    live.

    `psutil.Process.cpu_percent(interval=None)` reports the delta since the
    PREVIOUS call on that SAME Process object and returns 0.0 on a
    process's first call, so this class keeps one persistent
    `psutil.Process` per pid across samples rather than constructing a
    fresh one each time; a process that appears mid-capture (a new Chromium
    renderer) reads 0.0 CPU for its own first sample only, never after.
    """

    def __init__(self, root_pid: int) -> None:
        self._root_pid = root_pid
        self._tracked: dict[int, psutil.Process] = {}

    def _live_tree(self) -> list[psutil.Process]:
        try:
            root = psutil.Process(self._root_pid)
        except psutil.NoSuchProcess as exc:
            raise RuntimeError(
                f"mode_ratio_browser process {self._root_pid} is not running"
            ) from exc
        tree = [root, *root.children(recursive=True)]
        seen_pids = {proc.pid for proc in tree}
        for pid in list(self._tracked):
            if pid not in seen_pids:
                del self._tracked[pid]
        for proc in tree:
            if proc.pid not in self._tracked:
                self._tracked[proc.pid] = proc
                try:
                    proc.cpu_percent(interval=None)  # prime the delta baseline
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        return list(self._tracked.values())

    def sample(self) -> dict[str, float]:
        footprint_mb = 0.0
        cpu_percent = 0.0
        live = 0
        for proc in self._live_tree():
            try:
                footprint_mb += proc.memory_info().rss / (1024 * 1024)
                cpu_percent += proc.cpu_percent(interval=None)
                live += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if live == 0:
            raise RuntimeError(
                f"mode_ratio_browser process tree rooted at {self._root_pid} "
                "has no live processes to sample"
            )
        return {"physical_footprint_mb": footprint_mb, "cpu_percent": cpu_percent}


def _sample_steady(root_pid: int, duration_s: int) -> dict[str, float]:
    if duration_s < _MIN_SAMPLE_S:
        raise ValueError(f"duration must be at least {_MIN_SAMPLE_S}s, got {duration_s}")
    sampler = _ProcessTreeSampler(root_pid)
    sampler.sample()  # discard the primed-CPU first reading
    footprints: list[float] = []
    cpus: list[float] = []
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        time.sleep(_PROBE_INTERVAL_S)
        sample = sampler.sample()
        footprints.append(sample["physical_footprint_mb"])
        cpus.append(sample["cpu_percent"])
    if not footprints or not cpus:
        raise RuntimeError("probe returned no footprint or cpu samples")
    return {
        "footprint_mb": sum(footprints) / len(footprints),
        "cpu_percent": sum(cpus) / len(cpus),
        "sample_count": float(len(footprints)),
    }


_MIN_LEAK_DURATION_S = 3600


def _sample_leak(root_pid: int, duration_s: int) -> float:
    if duration_s < _MIN_LEAK_DURATION_S:
        raise ValueError(
            f"leak capture must run at least {_MIN_LEAK_DURATION_S}s (the requirement's "
            f"'1 h unattended' window), got {duration_s}s"
        )
    sampler = _ProcessTreeSampler(root_pid)
    sampler.sample()  # discard the primed-CPU first reading
    elapsed: list[float] = []
    footprints: list[float] = []
    start = time.monotonic()
    deadline = start + duration_s
    while time.monotonic() < deadline:
        time.sleep(_PROBE_INTERVAL_S)
        sample = sampler.sample()
        elapsed.append(time.monotonic() - start)
        footprints.append(sample["physical_footprint_mb"])
    slope_per_hour = _linear_slope_mb_per_hour(elapsed, footprints)
    if slope_per_hour is None:
        raise RuntimeError("leak capture produced no computable slope")
    return slope_per_hour / 6.0


def _read_browser_line(stream: IO[Any] | None, expected: str) -> None:
    if stream is None:
        raise RuntimeError("mode_ratio_browser stdout is not piped")
    line = stream.readline().strip()
    if line != expected:
        raise RuntimeError(f"mode_ratio_browser expected {expected!r}, got {line!r}")


def _signal_browser(proc: subprocess.Popen[str]) -> None:
    if proc.stdin is None:
        raise RuntimeError("mode_ratio_browser stdin is not piped")
    proc.stdin.write("NEXT\n")
    proc.stdin.flush()


def _start_browser_session(frontend: str, mode: str) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        ["node", str(_BROWSER_SCRIPT), "--frontend", frontend.rstrip("/"), "--mode", mode],
        cwd=_FRONTEND_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env={**os.environ, "AF_SERVICE_ID": _BROWSER_SERVICE_ID},
    )
    if proc.stdout is None:
        raise RuntimeError("mode_ratio_browser stdout is not piped")
    return proc


def _capture_gig_then_trackify(
    frontend: str, duration_s: int
) -> tuple[dict[str, float], dict[str, float]]:
    proc = _start_browser_session(frontend, "gig-trackify")
    try:
        _read_browser_line(proc.stdout, "GIG_READY")
        gig = _sample_steady(proc.pid, duration_s)
        _signal_browser(proc)
        _read_browser_line(proc.stdout, "TRACKIFY_READY")
        trackify = _sample_steady(proc.pid, duration_s)
        _signal_browser(proc)
        _read_browser_line(proc.stdout, "DONE")
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        code = proc.wait(timeout=30)
        if code != 0:
            raise RuntimeError(f"mode_ratio_browser exited {code}: {stderr.strip()}")
        return gig, trackify
    finally:
        if proc.poll() is None:
            proc.kill()


def _capture_trackify_leak(frontend: str, duration_s: int) -> float:
    proc = _start_browser_session(frontend, "trackify-leak")
    try:
        _read_browser_line(proc.stdout, "TRACKIFY_READY")
        slope = _sample_leak(proc.pid, duration_s)
        _signal_browser(proc)
        _read_browser_line(proc.stdout, "DONE")
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        code = proc.wait(timeout=30)
        if code != 0:
            raise RuntimeError(f"mode_ratio_browser exited {code}: {stderr.strip()}")
        return slope
    finally:
        if proc.poll() is None:
            proc.kill()


_METHOD = (
    "process-tree RSS/CPU sampling of the Playwright-launched Chromium running "
    "Gig/Trackify (PERFMODE-15) -- browser/renderer process family only, same scope "
    "as capture_library_mode.py's PERFMODE-14 ratios, not the packaged app's "
    "telemetry endpoint and not the python engine (no per-process CPU is exposed "
    "for that family by any existing endpoint)"
)


def main(argv: list[str] | None = None) -> int:
    _require_macos()
    parser = argparse.ArgumentParser(description="Capture Trackify mode KPI ratios")
    parser.add_argument("--mode", choices=["trackify"], required=True)
    parser.add_argument("--gig-baseline", action="store_true")
    parser.add_argument("--frontend", type=str, required=True)
    parser.add_argument("--duration-s", type=int, default=_MIN_SAMPLE_S)
    parser.add_argument("--leak-duration-s", type=int, default=0)
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args(argv)

    if not _BROWSER_SCRIPT.is_file():
        raise SystemExit(f"mode_ratio_browser helper missing: {_BROWSER_SCRIPT}")

    meta = session_meta(sha=_git_sha())
    rows: list[dict[str, Any]] = []

    if args.gig_baseline:
        gig, trackify = _capture_gig_then_trackify(args.frontend, args.duration_s)
        if gig["footprint_mb"] <= 0 or gig["cpu_percent"] <= 0:
            raise SystemExit("gig baseline denominators missing or zero; refusing ratio write")
        footprint_ratio = 1.0 - (trackify["footprint_mb"] / gig["footprint_mb"])
        cpu_ratio = 1.0 - (trackify["cpu_percent"] / gig["cpu_percent"])
        note = (
            f"PERFMODE-15 trackify ratios; gig_fp={gig['footprint_mb']:.2f}MB "
            f"trackify_fp={trackify['footprint_mb']:.2f}MB"
        )
        rows.extend(
            [
                build_row(
                    kpi="trackify_mode_footprint_ratio",
                    value=round(footprint_ratio, 4),
                    unit="ratio",
                    method=_METHOD,
                    meta=meta,
                    note=note,
                    measured=True,
                ),
                build_row(
                    kpi="trackify_mode_cpu_ratio",
                    value=round(cpu_ratio, 4),
                    unit="ratio",
                    method=_METHOD,
                    meta=meta,
                    note=note,
                    measured=True,
                ),
            ]
        )

    if args.leak_duration_s > 0:
        slope = _capture_trackify_leak(args.frontend, args.leak_duration_s)
        rows.append(
            build_row(
                kpi="trackify_mode_footprint_slope_mb_per_10min",
                value=round(slope, 4),
                unit="MB/10min",
                method=_METHOD,
                meta=meta,
                note=f"PERFMODE-15 trackify 1h leak slope over {args.leak_duration_s}s",
                measured=True,
            )
        )

    if not rows:
        raise SystemExit("no capture requested: pass --gig-baseline and/or --leak-duration-s")

    append_ledger_rows(args.ledger, rows)
    print(json.dumps({"capture_id": meta.capture_id, "rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
