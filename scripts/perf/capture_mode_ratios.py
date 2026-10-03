"""Capture Trackify vs Gig steady-state ratios and 1h leak slopes (PERFMODE-15).

The leak run records two slopes (ADR-NEW-trackify-leak-kpi-quiescent-baselines):
the gating `trackify_mode_retained_slope_mb_per_10min`, fitted to quiescent
baselines (workload undone, garbage collected), and the diagnostic raw
`trackify_mode_footprint_slope_mb_per_10min`, fitted to the samples taken while
a track plays. `--leak-series-out` keeps every sample as a TSV.

The ratio rows (`--gig-baseline`) are over PERFMODE-14's process family: every
Chromium process plus the engine at `--engine` and its descendants; the
browser-only ratios ride along in the row note as a diagnostic.

macOS reference capture only. Refuses Linux hosts with an explicit message.

Usage (reference Mac, engine + frontend already running):
  uv run python -m scripts.perf.capture_mode_ratios --mode trackify --gig-baseline \\
    --frontend http://127.0.0.1:8791 --engine http://127.0.0.1:8791 --duration-s 60 --ledger docs/perf/kpi-ledger.json
  uv run python -m scripts.perf.capture_mode_ratios --mode trackify --leak-duration-s 3600 \\
    --frontend http://127.0.0.1:5273 --ledger docs/perf/kpi-ledger.json --leak-series-out series.tsv
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from scripts.perf.capture_build_identity import _REPO, _git_sha
from scripts.perf.capture_kpi_ledger import session_meta
from scripts.perf.mode_ratio_identity import (
    _append_rows_after_reverification,
    _capture_identity_reason,
)
from scripts.perf.mode_ratio_engine import EngineTarget, verify_engine_target
from scripts.perf.mode_ratio_sampler import _ProcessTreeSampler
from scripts.perf.perfmode14_scorer import (
    _MIN_SCORED_SAMPLES,
    perfmode14_median,
    perfmode14_medians_from_lists,
)
from scripts.perf.mode_ratio_rows import gig_baseline_rows as _gig_baseline_rows
from scripts.perf.mode_ratio_rows import leak_rows as _leak_rows
from scripts.perf.mode_ratio_rows import validate_gig_stable_ids as _validate_gig_stable_ids
from scripts.perf.node_runtime import resolved_node
from scripts.perf.trackify_leak_series import (
    CHECKPOINT_SAMPLE_GAP_S,
    CHECKPOINT_SAMPLES,
    LeakSeries,
    median_baseline_mb,
    next_checkpoint_due,
)

_FRONTEND_ROOT = _REPO / "apps" / "webui" / "frontend"
_BROWSER_SCRIPT = _REPO / "scripts" / "perf" / "mode_ratio_browser.mjs"
_MIN_SAMPLE_S = 60
# Twelve samples fit in the 60 s minimum dwell at PERFMODE-14's 5 s cadence (KPI_CAPTURE_SAMPLE_INTERVAL_S default).
_PROBE_INTERVAL_S = 5
_BROWSER_EXIT_TIMEOUT_S = 30
_BROWSER_SERVICE_ID = "com.af.music-dj-tools.mode-ratio-browser"


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise SystemExit(
            "capture_mode_ratios refuses non-macOS capture: run on the reference Mac "
            "with opendj_performance_probe installed"
        )


_STEADY_MEANS = (
    ("footprint_mb", "physical_footprint_mb"),
    ("cpu_percent", "cpu_percent"),
    ("browser_footprint_mb", "browser_footprint_mb"),
    ("browser_cpu_percent", "browser_cpu_percent"),
    ("engine_footprint_mb", "engine_footprint_mb"),
    ("engine_cpu_percent", "engine_cpu_percent"),
)


def _sample_steady(root_pid: int, duration_s: int, engine_root_pid: int | None) -> dict[str, float]:
    """Median of every `_PROBE_INTERVAL_S` sample over `duration_s` (PERFMODE-14 scorer).

    Footprint uses phys_footprint; CPU is `/bin/ps` `%cpu` summed over the
    process family (PERFMODE-14). The engine fields appear only when an engine root is given.
    """
    min_duration = max(_MIN_SAMPLE_S, _MIN_SCORED_SAMPLES * _PROBE_INTERVAL_S)
    if duration_s < min_duration:
        raise ValueError(f"duration must be at least {min_duration}s, got {duration_s}")
    sampler = _ProcessTreeSampler(root_pid, engine_root_pid=engine_root_pid)
    samples: list[dict[str, float]] = []
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        time.sleep(_PROBE_INTERVAL_S)
        samples.append(sampler.sample())
    if not samples:
        raise RuntimeError("probe returned no footprint or cpu samples")
    footprint_series = [s["physical_footprint_mb"] for s in samples]
    cpu_series = [s["cpu_percent"] for s in samples]
    perfmode14_medians_from_lists(
        footprint_series, cpu_series, mode=f"steady pid={root_pid}"
    )
    steady = {
        key: perfmode14_median([s[field] for s in samples]) for key, field in _STEADY_MEANS
    }
    steady["sample_count"] = float(len(samples))
    if engine_root_pid is None:
        return {key: value for key, value in steady.items() if not key.startswith("engine_")}
    steady["engine_pid_count_max"] = max(s["engine_pid_count"] for s in samples)
    return steady


_MIN_LEAK_DURATION_S = 3600


def _send_browser_line(proc: subprocess.Popen[str], line: str) -> None:
    if proc.stdin is None:
        raise RuntimeError("mode_ratio_browser stdin is not piped")
    proc.stdin.write(line + "\n")
    proc.stdin.flush()


def _quiescent_baseline_mb(proc: subprocess.Popen[str], sampler: _ProcessTreeSampler) -> float:
    """One quiescent checkpoint: the helper undoes the workload and collects garbage
    (QUIESCENT), the median of CHECKPOINT_SAMPLES footprints is the baseline, then
    playback resumes (RESUMED). ADR-NEW-trackify-leak-kpi-quiescent-baselines."""
    _send_browser_line(proc, "CHECKPOINT")
    _read_browser_line(proc, "QUIESCENT")
    samples: list[float] = []
    for index in range(CHECKPOINT_SAMPLES):
        if index > 0:
            time.sleep(CHECKPOINT_SAMPLE_GAP_S)
        samples.append(sampler.sample()["physical_footprint_mb"])
    _send_browser_line(proc, "RESUME")
    _read_browser_line(proc, "RESUMED")
    return median_baseline_mb(samples)


def _sample_leak(proc: subprocess.Popen[str], duration_s: int) -> LeakSeries:
    """Raw samples every _PROBE_INTERVAL_S while Trackify plays, and a quiescent
    baseline every CHECKPOINT_INTERVAL_S of played time (wall time minus time
    spent quiescent), from played time 0 through `duration_s`."""
    if duration_s < _MIN_LEAK_DURATION_S:
        raise ValueError(
            f"leak capture must run at least {_MIN_LEAK_DURATION_S}s (the requirement's "
            f"'1 h unattended' window), got {duration_s}s"
        )
    sampler = _ProcessTreeSampler(proc.pid)
    series = LeakSeries(duration_s=float(duration_s))
    start = time.monotonic()
    next_due = 0.0
    while True:
        checkpoint_started = time.monotonic()
        played = checkpoint_started - start - series.quiescent_s
        if played >= next_due:
            baseline = _quiescent_baseline_mb(proc, sampler)
            series.quiescent_s += time.monotonic() - checkpoint_started
            series.baselines.append((played, baseline))
            if played >= duration_s:
                break
            next_due = next_checkpoint_due(next_due, duration_s)
            continue
        time.sleep(_PROBE_INTERVAL_S)
        sample = sampler.sample()
        series.raw.append((time.monotonic() - start - series.quiescent_s, sample["physical_footprint_mb"]))
    series.require_complete()
    print(f"leak {series.summary()}", file=sys.stderr)
    return series


def _read_gig_stable_ids(proc: subprocess.Popen[str]) -> list[str]:
    """Read and validate the `GIG_STABLE_IDS <json>` protocol line.

    Mirrors `_valid_deck_stable_ids` in capture_library_mode.py: a Trackify row
    that cannot name which four tracks backed its Gig denominator is not an
    auditable measurement (Codex, discussion_r4138216697).
    """
    if proc.stdout is None:
        raise RuntimeError("mode_ratio_browser stdout is not piped")
    line = proc.stdout.readline().strip()
    prefix = "GIG_STABLE_IDS "
    if not line.startswith(prefix):
        raise RuntimeError(
            f"mode_ratio_browser expected 'GIG_STABLE_IDS ...', got {line!r}; "
            f"helper stderr: {_browser_stderr_tail(proc)}"
        )
    try:
        ids = json.loads(line[len(prefix) :])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GIG_STABLE_IDS payload is not valid JSON: {line!r}") from exc
    return _validate_gig_stable_ids(ids)


def _read_browser_line(proc: subprocess.Popen[str], expected: str) -> None:
    if proc.stdout is None:
        raise RuntimeError("mode_ratio_browser stdout is not piped")
    line = proc.stdout.readline().strip()
    if line != expected:
        raise RuntimeError(
            f"mode_ratio_browser expected {expected!r}, got {line!r}; "
            f"helper stderr: {_browser_stderr_tail(proc)}"
        )


_SETTLE_S_PREFIX = "SETTLE_S "


def _read_phase_ready(proc: subprocess.Popen[str], ready: str) -> float | None:
    """Read optional ``SETTLE_S <n>`` then ``ready`` (e.g. GIG_READY).

    When the helper omits SETTLE_S, returns None and does not set settle_s on
    the phase dict downstream.
    """
    if proc.stdout is None:
        raise RuntimeError("mode_ratio_browser stdout is not piped")
    line = proc.stdout.readline().strip()
    settle_s: float | None = None
    if line.startswith(_SETTLE_S_PREFIX):
        raw = line[len(_SETTLE_S_PREFIX) :].strip()
        try:
            settle_s = float(raw)
        except ValueError as exc:
            raise RuntimeError(f"SETTLE_S payload is not numeric: {line!r}") from exc
        if settle_s < 0:
            raise RuntimeError(f"SETTLE_S must be non-negative, got {settle_s}")
        line = proc.stdout.readline().strip()
    if line != ready:
        raise RuntimeError(
            f"mode_ratio_browser expected {ready!r}, got {line!r}; "
            f"helper stderr: {_browser_stderr_tail(proc)}"
        )
    return settle_s


def _attach_settle_s(phase: dict[str, float], settle_s: float | None) -> None:
    if settle_s is not None:
        phase["settle_s"] = settle_s


def _browser_stderr_tail(proc: subprocess.Popen[str]) -> str:
    """The browser helper's own error, which a bare "expected X, got ''" hides."""
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=30)
    stderr = proc.stderr.read() if proc.stderr is not None else ""
    return stderr.strip()[-2000:] or "<empty stderr>"


def _close_browser_stdin(proc: subprocess.Popen[str]) -> None:
    """EOF the helper's stdin once its protocol is finished.

    Node keeps a process alive while its stdin pipe is open and referenced, so
    after DONE the helper never exits, `stderr.read()` never sees EOF, and a
    finished capture hangs forever without writing a row (hit live on silver,
    Fri 25 Sep 2026).
    """
    if proc.stdin is None:
        raise RuntimeError("mode_ratio_browser stdin is not piped")
    proc.stdin.close()


def _signal_browser(proc: subprocess.Popen[str]) -> None:
    _send_browser_line(proc, "NEXT")


def _finish_browser_session(
    proc: subprocess.Popen[str], timeout_s: float = _BROWSER_EXIT_TIMEOUT_S
) -> None:
    """Read DONE, EOF the helper's stdin, and require a clean exit.

    Closing stdin comes before reading stderr: a helper that lives until stdin
    EOF would otherwise never exit and the read would block forever.

    `timeout_s` defaults to `_BROWSER_EXIT_TIMEOUT_S` -- the real, shipped
    value every production caller gets -- but is a genuine parameter rather
    than a module constant a test would otherwise have to monkeypatch (Codex
    P1/BLOCKING, PR #4034, discussion_r4138422261: AGENTS.md's "No mocks and
    locked real fixtures" contract forbids patching module state in a test
    even to exercise an "unrelated" timeout path; a caller-supplied override
    exercises the SAME code this function ships with, just with a shorter,
    real number).
    """
    _read_browser_line(proc, "DONE")
    _close_browser_stdin(proc)
    try:
        code = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout_s)
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        detail = stderr.strip() or "<empty stderr>"
        raise RuntimeError(
            f"mode_ratio_browser did not exit within {timeout_s}s after stdin EOF: {detail}"
        ) from None
    stderr = proc.stderr.read() if proc.stderr is not None else ""
    if code != 0:
        raise RuntimeError(f"mode_ratio_browser exited {code}: {stderr.strip()}")


def _start_browser_session(frontend: str, mode: str) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [resolved_node(), str(_BROWSER_SCRIPT), "--frontend", frontend.rstrip("/"), "--mode", mode],
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
    frontend: str, duration_s: int, engine_root_pid: int
) -> tuple[dict[str, float], dict[str, float], list[str]]:
    proc = _start_browser_session(frontend, "gig-trackify")
    try:
        gig_stable_ids = _read_gig_stable_ids(proc)
        gig_settle_s = _read_phase_ready(proc, "GIG_READY")
        gig = _sample_steady(proc.pid, duration_s, engine_root_pid)
        _attach_settle_s(gig, gig_settle_s)
        _signal_browser(proc)
        trackify_settle_s = _read_phase_ready(proc, "TRACKIFY_READY")
        trackify = _sample_steady(proc.pid, duration_s, engine_root_pid)
        _attach_settle_s(trackify, trackify_settle_s)
        _signal_browser(proc)
        _finish_browser_session(proc)
        return gig, trackify, gig_stable_ids
    finally:
        if proc.poll() is None:
            proc.kill()


def _capture_trackify_leak(frontend: str, duration_s: int) -> LeakSeries:
    proc = _start_browser_session(frontend, "trackify-leak")
    try:
        _read_phase_ready(proc, "TRACKIFY_READY")
        series = _sample_leak(proc, duration_s)
        _signal_browser(proc)
        _finish_browser_session(proc)
        return series
    finally:
        if proc.poll() is None:
            proc.kill()


def main(argv: list[str] | None = None) -> int:
    _require_macos()
    parser = argparse.ArgumentParser(description="Capture Trackify mode KPI ratios")
    parser.add_argument("--mode", choices=["trackify"], required=True)
    parser.add_argument("--gig-baseline", action="store_true")
    parser.add_argument("--frontend", type=str, required=True)
    parser.add_argument(
        "--engine",
        type=str,
        default=None,
        help="engine origin the page's /api reaches; required with --gig-baseline (PERFMODE-14 family)",
    )
    parser.add_argument("--duration-s", type=int, default=_MIN_SAMPLE_S)
    parser.add_argument("--leak-duration-s", type=int, default=0)
    parser.add_argument("--leak-series-out", type=Path, default=None)
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args(argv)

    if not _BROWSER_SCRIPT.is_file():
        raise SystemExit(f"mode_ratio_browser helper missing: {_BROWSER_SCRIPT}")

    # Identity gate (Sol P1/BLOCKING, PR #4034, discussion_r4139047412): unlike
    # capture_library_mode.py, this capture previously labeled its rows with
    # the LOCAL checkout's sha while never confirming the frontend actually
    # serving `--frontend` was a clean, matching static build -- a stale,
    # dirty, or different checkout would be measured and mislabeled as the
    # local sha with no way to tell from the ledger row alone. Reuse the same
    # checks capture_library_targets.py's `_verify_capture_targets` runs for
    # PERFMODE-14, scoped to what this capture actually has (no engine target
    # here, only a served frontend).
    sha = _git_sha()
    identity_reason = _capture_identity_reason(args.frontend, sha)
    if identity_reason is not None:
        raise SystemExit(f"refusing to capture: {identity_reason}")

    engine: EngineTarget | None = None
    if args.gig_baseline:
        if args.engine is None:
            parser.error("--gig-baseline needs --engine: the ratios include the engine family")
        engine, engine_reason = verify_engine_target(args.engine, args.frontend, sha)
        if engine_reason is not None:
            raise SystemExit(f"refusing to capture: {engine_reason}")

    meta = session_meta(sha=sha)
    rows: list[dict[str, Any]] = []
    leak_series_out: tuple[LeakSeries, Path] | None = None

    if engine is not None:
        gig, trackify, gig_stable_ids = _capture_gig_then_trackify(
            args.frontend, args.duration_s, engine.pid
        )
        print(f"gig {json.dumps(gig)}\ntrackify {json.dumps(trackify)}", file=sys.stderr)
        rows.extend(_gig_baseline_rows(gig, trackify, gig_stable_ids, meta))

    if args.leak_duration_s > 0:
        series = _capture_trackify_leak(args.frontend, args.leak_duration_s)
        leak_series_out = None if args.leak_series_out is None else (series, args.leak_series_out)
        rows.extend(_leak_rows(series, meta))

    if not rows:
        raise SystemExit("no capture requested: pass --gig-baseline and/or --leak-duration-s")

    _append_rows_after_reverification(args.ledger, rows, args.frontend, sha, engine=engine, leak_series_out=leak_series_out)
    print(json.dumps({"capture_id": meta.capture_id, "rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
