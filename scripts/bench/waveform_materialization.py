#!/usr/bin/env python3
"""Benchmark Python vs release PyO3 ANLZ waveform materialization.

Both kernels receive the same band arrays decoded by rb_vendor from a real
Rekordbox ANLZ directory. Timings are repeated and interleaved; peak RSS is
measured in isolated child processes so the first kernel cannot contaminate
the second kernel's high-water mark.

THE VERDICT READS PROCESS CPU TIME, NOT ELAPSED WALL TIME. Both kernels are
CPU-bound and single-threaded (the PyO3 side releases the GIL for the
downsample and fans out to nothing), so their cost is CPU seconds. Gating on
elapsed time instead measured this box's other tenants: on Mon 31 Aug 2026 one
commit scored 6.28, 14.29 and 33.53 percent improvement across three runs while
the load average climbed 42 -> 68, and every local agent that hit the red paid
for a regression that was not in the diff. ``median_cpu_ms`` and the
``rust_improvement_pct`` derived from it are therefore the gated numbers, and
the thresholds themselves are unchanged: 30 percent median improvement and a
peak-RSS ratio at or under 1.10.

``median_wall_ms`` and the whole ``same_process_threads`` block are REPORT ONLY
and deliberately still wall-clock. Thread throughput is a concurrency figure;
CPU-time it and it stops meaning anything. Read them as a description of the
machine the run met, never as a verdict, and do not add either to ``targets``.

Acceptance tests (tests/scripts/test_bench_timing.py cover the instrument):
  [if] the box is loaded with competing CPU hogs
       [then] the gated medians move an order of magnitude less than the wall
       medians beside them
  [if] the Rust payload differs from the Python payload
       [then] refuse to report a speed number at all
  [if] the CPU clock cannot resolve the Python kernel
       [then] raise rather than divide by zero into a fake improvement
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.webui.server import rb_vendor  # noqa: E402
from scripts.bench.timing import Sample, interleaved_samples  # noqa: E402
from tests.fixtures._resolver import (  # noqa: E402
    FixtureNotAvailable,
    fixture_path,
    verify_fixture_contract,
)


def _default_fixture() -> Path | None:
    """Resolve the default ANLZ fixture through the resolver, wherever it lives.

    Returns ``None`` (rather than a stale hard-coded repo path) when the
    fixture host isn't available, so ``--anlz-dir`` still works and a
    no-argument invocation fails with a clear message instead of a
    FileNotFoundError deep inside rb_vendor.

    A resolved fixture's content is checked against
    ``tests/fixtures/rb-usb-export.contract.json`` before use, so a stale
    or partially copied ``MUX_FIXTURE_HOST`` tree crashes loudly here
    (``FixtureContractMismatch`` propagates uncaught) rather than silently
    producing a timing result that is not comparable to prior canonical-
    fixture runs.
    """
    try:
        root = fixture_path("rb-usb-export")
    except (FixtureNotAvailable, FileNotFoundError):
        return None
    verify_fixture_contract("rb-usb-export", root)
    return root / "PIONEER" / "USBANLZ" / "P000" / "00029138"


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


def _medians(samples: dict[str, list[Sample]], field: str) -> dict[str, float]:
    """Per-arm median of one Sample field, in milliseconds."""
    return {
        name: statistics.median(getattr(sample, field) for sample in values)
        for name, values in samples.items()
    }


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
    parser.add_argument("--anlz-dir", type=Path, default=None)
    parser.add_argument("--points", type=int, default=38400)
    parser.add_argument("--iterations", type=int, default=31)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--thread-calls", type=int, default=12)
    parser.add_argument("--thread-rounds", type=int, default=5)
    parser.add_argument("--rss-worker", choices=("python", "rust"))
    args = parser.parse_args()

    # Resolve (and contract-verify) the canonical default only when the
    # caller omitted --anlz-dir, so a stale/partial MUX_FIXTURE_HOST tree
    # cannot break an invocation that passed its own directory, and an
    # --rss-worker subprocess (always passed --anlz-dir explicitly by
    # _isolated_peak_rss) never re-hashes the default fixture it doesn't
    # need (PR #718 review).
    if args.anlz_dir is None:
        args.anlz_dir = _default_fixture()
    if args.anlz_dir is None:
        raise RuntimeError(
            "rb-usb-export fixture host not available and no --anlz-dir given. "
            "Mount the fixture host (MUX_FIXTURE_HOST) or pass --anlz-dir explicitly."
        )

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

    samples = interleaved_samples(
        {name: partial(kernel, bands, args.points) for name, kernel in kernels.items()},
        args.iterations,
    )
    cpu_medians = _medians(samples, "cpu_ms")
    wall_medians = _medians(samples, "wall_ms")
    if cpu_medians["python"] <= 0.0:
        raise RuntimeError(
            "the Python kernel measured 0ms of CPU time, so no improvement is computable; "
            f"process_time resolution here is {time.get_clock_info('process_time').resolution}s "
            "- raise --points until the kernel outruns the clock"
        )
    improvement = (cpu_medians["python"] - cpu_medians["rust"]) / cpu_medians["python"] * 100.0
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
        "timing_basis": (
            "median_cpu_ms and rust_improvement_pct are process CPU time and are what "
            "`pass` reads; median_wall_ms and same_process_threads are elapsed wall time, "
            "report only, and move with whatever else is running on this box"
        ),
        "median_cpu_ms": cpu_medians,
        "median_wall_ms": wall_medians,
        "rust_improvement_pct": improvement,
        "peak_rss": rss,
        "rust_peak_rss_ratio": rss_ratio,
        "same_process_threads": {
            "workers": args.threads,
            "calls_per_round": args.thread_calls,
            "rounds": args.thread_rounds,
            "basis": "elapsed wall time, report only - a concurrency figure has no CPU-time form",
            "median_requests_per_second": threaded_medians,
            "throughput_vs_single_call": {
                name: threaded_medians[name] / (1000.0 / wall_medians[name]) for name in kernels
            },
        },
        "targets": {"median_improvement_pct": 30.0, "peak_rss_ratio": 1.10},
        "pass": improvement >= 30.0 and rss_ratio <= 1.10,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
