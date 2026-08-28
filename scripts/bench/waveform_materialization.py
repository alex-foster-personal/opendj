#!/usr/bin/env python3
"""Benchmark Python vs release PyO3 ANLZ waveform materialization.

Both kernels receive the same band arrays decoded by rb_vendor from a real
Rekordbox ANLZ directory. Timings are repeated and interleaved; peak RSS is
measured in isolated child processes so the first kernel cannot contaminate
the second kernel's high-water mark.
"""

from __future__ import annotations

import argparse
import gc
import json
import resource
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.webui.server import rb_vendor  # noqa: E402

DEFAULT_FIXTURE = REPO_ROOT / "tests/fixtures/rb-usb-export/PIONEER/USBANLZ/P000/00029138"


def _bands(anlz_dir: Path) -> dict[str, Any]:
    tags, unreadable = rb_vendor._first_tags(anlz_dir)
    if unreadable:
        raise RuntimeError(f"benchmark ANLZ directory has unreadable files: {unreadable}")
    if "PWV7" not in tags:
        raise RuntimeError(f"benchmark ANLZ directory has no PWV7 detail bands: {anlz_dir}")
    return rb_vendor._tri_bands(tags["PWV7"])


def _kernels() -> dict[str, Callable[[dict[str, Any], int], dict[str, Any]]]:
    native = rb_vendor._WAVEFORM_NATIVE
    if native is None:
        raise RuntimeError(
            "release _rb_waveform_native extension unavailable; "
            "build the waveform crate with maturin"
        )
    return {
        "python": rb_vendor._bands_payload_python,
        "rust": native.bands_payload,
    }


def _peak_rss() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _rss_worker(backend: str, anlz_dir: Path, points: int, iterations: int) -> None:
    bands = _bands(anlz_dir)
    kernel = _kernels()[backend]
    for _ in range(iterations):
        payload = kernel(bands, points)
        if payload["length"] == 0:
            raise RuntimeError("benchmark unexpectedly materialized an empty waveform")
    print(json.dumps({"backend": backend, "peak_rss": _peak_rss()}))


def _isolated_peak_rss(backend: str, anlz_dir: Path, points: int, iterations: int) -> int:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--anlz-dir",
        str(anlz_dir),
        "--points",
        str(points),
        "--iterations",
        str(iterations),
        "--rss-worker",
        backend,
    ]
    completed = subprocess.run(command, check=True, text=True, capture_output=True)
    return int(json.loads(completed.stdout.strip().splitlines()[-1])["peak_rss"])


def _threaded_throughput(
    kernel: Callable[[dict[str, Any], int], dict[str, Any]],
    bands: dict[str, Any],
    points: int,
    *,
    workers: int,
    calls: int,
) -> float:
    """Measure overlapping sync-route work inside one Python process."""

    def invoke(_index: int) -> int:
        return int(kernel(bands, points)["length"])

    started = time.perf_counter_ns()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        lengths = list(executor.map(invoke, range(calls)))
    elapsed_s = (time.perf_counter_ns() - started) / 1_000_000_000
    if not lengths or any(length == 0 for length in lengths):
        raise RuntimeError("threaded benchmark unexpectedly materialized an empty waveform")
    return calls / elapsed_s


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anlz-dir", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--points", type=int, default=38400)
    parser.add_argument("--iterations", type=int, default=31)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--thread-calls", type=int, default=12)
    parser.add_argument("--thread-rounds", type=int, default=5)
    parser.add_argument("--rss-worker", choices=("python", "rust"))
    args = parser.parse_args()

    if args.rss_worker:
        _rss_worker(args.rss_worker, args.anlz_dir, args.points, args.iterations)
        return 0

    bands = _bands(args.anlz_dir)
    kernels = _kernels()
    expected = kernels["python"](bands, args.points)
    actual = kernels["rust"](bands, args.points)
    if actual != expected:
        raise RuntimeError("Rust waveform payload differs from Python production payload")

    for _ in range(args.warmup):
        for kernel in kernels.values():
            kernel(bands, args.points)

    timings: dict[str, list[float]] = {"python": [], "rust": []}
    gc.disable()
    try:
        for iteration in range(args.iterations):
            order = ("python", "rust") if iteration % 2 == 0 else ("rust", "python")
            for backend in order:
                started = time.perf_counter_ns()
                kernels[backend](bands, args.points)
                elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
                timings[backend].append(elapsed_ms)
    finally:
        gc.enable()

    medians = {name: statistics.median(values) for name, values in timings.items()}
    improvement = (medians["python"] - medians["rust"]) / medians["python"] * 100.0
    threaded: dict[str, list[float]] = {"python": [], "rust": []}
    for round_index in range(args.thread_rounds):
        order = ("python", "rust") if round_index % 2 == 0 else ("rust", "python")
        for backend in order:
            threaded[backend].append(
                _threaded_throughput(
                    kernels[backend],
                    bands,
                    args.points,
                    workers=args.threads,
                    calls=args.thread_calls,
                )
            )
    threaded_medians = {name: statistics.median(values) for name, values in threaded.items()}
    rss = {
        name: _isolated_peak_rss(name, args.anlz_dir, args.points, args.iterations)
        for name in ("python", "rust")
    }
    rss_ratio = rss["rust"] / rss["python"]
    report = {
        "fixture": str(args.anlz_dir),
        "band_length": len(next(iter(bands.values()))),
        "points": args.points,
        "iterations": args.iterations,
        "median_ms": medians,
        "rust_improvement_pct": improvement,
        "peak_rss": rss,
        "rust_peak_rss_ratio": rss_ratio,
        "same_process_threads": {
            "workers": args.threads,
            "calls_per_round": args.thread_calls,
            "rounds": args.thread_rounds,
            "median_requests_per_second": threaded_medians,
            "throughput_vs_single_call": {
                name: threaded_medians[name] / (1000.0 / medians[name]) for name in kernels
            },
        },
        "targets": {"median_improvement_pct": 30.0, "peak_rss_ratio": 1.10},
        "pass": improvement >= 30.0 and rss_ratio <= 1.10,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
