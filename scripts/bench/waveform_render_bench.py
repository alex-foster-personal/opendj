#!/usr/bin/env python3
"""Measurement lane for native waveform materialization (_rb_waveform_native).

Prints medians and writes samples. Never fails on a slow number; exit 3 is
UNKNOWN when the native extension or fixture bands are unavailable. Exit 1
means the harness broke (payload mismatch, empty output).

Measured over: one deterministic tri-band ANLZ-shaped payload at POINTS
downsampled by the production Rust kernel, repeated INTERLEAVED_ITERATIONS
times, plus a same-process 4-thread throughput block of THREAD_CALLS calls.

Run::

    uv run --no-sync python scripts/bench/waveform_render_bench.py

Acceptance tests: tests/scripts/test_waveform_render_bench.py
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.webui.server import rb_vendor  # noqa: E402
from scripts.bench.timing import interleaved_samples  # noqa: E402

POINTS = 38_400
BAND_LENGTH = 8_192
INTERLEAVED_ITERATIONS = 21
WARMUP = 3
THREADS = 4
THREAD_CALLS = 12
THREAD_ROUNDS = 5
DEFAULT_OUT = (
    REPO_ROOT / "apps/webui/frontend/tests/e2e/fixtures/waveform-render.json"
)


def _host_identity() -> str:
    return socket.gethostname()


def _git_sha() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return "UNKNOWN"
    return completed.stdout.strip()


def _rust_toolchain() -> str:
    completed = subprocess.run(
        ["rustc", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return "UNKNOWN"
    return completed.stdout.strip()


def _fixture_bands() -> dict[str, np.ndarray]:
    """Deterministic tri-band arrays sized like a detail ANLZ band set."""
    bands: dict[str, np.ndarray] = {}
    for index, name in enumerate(("low", "mid", "high")):
        base = float(index + 1)
        bands[name] = np.array(
            [
                min(1.0, (base + (offset % 17) * 0.73 + (offset // 17) * 0.11) / 127.0)
                for offset in range(BAND_LENGTH)
            ],
            dtype=np.float64,
        )
    return bands


def _native_kernel() -> Callable[[dict[str, np.ndarray], int], dict[str, object]]:
    native = rb_vendor._WAVEFORM_NATIVE
    if native is None:
        print(
            "UNKNOWN: release _rb_waveform_native extension unavailable; "
            "run `make waveform-native-wheel` and reinstall",
            file=sys.stderr,
        )
        raise SystemExit(3)
    return native.bands_payload


def _threaded_throughput(
    kernel: Callable[[dict[str, np.ndarray], int], dict[str, object]],
    bands: dict[str, np.ndarray],
    *,
    workers: int,
    calls: int,
) -> float:
    def invoke(_index: int) -> int:
        length = kernel(bands, POINTS)["length"]
        if not isinstance(length, int):
            print("[ERROR] threaded benchmark returned a non-integer length", file=sys.stderr)
            raise SystemExit(1)
        return length

    started_ns = time.perf_counter_ns()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        lengths = list(executor.map(invoke, range(calls)))
    elapsed_s = (time.perf_counter_ns() - started_ns) / 1_000_000_000
    if not lengths or any(length == 0 for length in lengths):
        print("[ERROR] threaded benchmark materialized an empty waveform", file=sys.stderr)
        raise SystemExit(1)
    return calls / elapsed_s


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--points", type=int, default=POINTS)
    parser.add_argument("--iterations", type=int, default=INTERLEAVED_ITERATIONS)
    args = parser.parse_args()

    bands = _fixture_bands()
    kernel = _native_kernel()
    python = rb_vendor._bands_payload_python
    expected = python(bands, args.points)
    actual = kernel(bands, args.points)
    if actual != expected:
        print(
            "[ERROR] Rust waveform payload differs from Python production payload",
            file=sys.stderr,
        )
        return 1

    for _ in range(WARMUP):
        kernel(bands, args.points)

    samples = interleaved_samples(
        {"rust": partial(kernel, bands, args.points)},
        args.iterations,
    )
    cpu_values = [sample.cpu_ms for sample in samples["rust"]]
    wall_values = [sample.wall_ms for sample in samples["rust"]]
    threaded = [
        _threaded_throughput(
            kernel,
            bands,
            workers=THREADS,
            calls=THREAD_CALLS,
        )
        for _ in range(THREAD_ROUNDS)
    ]

    report = {
        "host": _host_identity(),
        "git_sha": _git_sha(),
        "rust_toolchain": _rust_toolchain(),
        "denominator": (
            f"one synthetic tri-band payload ({BAND_LENGTH} columns/band) materialized "
            f"to {args.points} points by _rb_waveform_native.bands_payload, "
            f"{args.iterations} interleaved timed calls, "
            f"4-thread throughput over {THREAD_CALLS} calls x {THREAD_ROUNDS} rounds"
        ),
        "band_length": BAND_LENGTH,
        "points": args.points,
        "iterations": args.iterations,
        "samples": {
            "median_cpu_ms": statistics.median(cpu_values),
            "median_wall_ms": statistics.median(wall_values),
            "median_requests_per_second": statistics.median(threaded),
        },
        "raw": {
            "cpu_ms": cpu_values,
            "wall_ms": wall_values,
            "requests_per_second": threaded,
        },
        "noise_note": (
            "Same SHA and host on a quiet nucbox-wsl runner: expect "
            "median_requests_per_second within roughly 20% run-to-run; "
            "median_cpu_ms can spread wider when values are sub-millisecond "
            "and the host is contended; wall_ms is report-only"
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(f"{json.dumps(report, indent=2, sort_keys=True)}\n", encoding="utf-8")
    print(json.dumps(report["samples"], indent=2, sort_keys=True))
    print(f"[waveform-render-bench] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
