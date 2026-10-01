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
import sys
import time
from pathlib import Path
from typing import Any

import psutil

from scripts.diagnostics.probe_log_store import _linear_slope_mb_per_hour
from scripts.diagnostics.probe_native_metrics import DarwinProcessMetrics
from scripts.perf.capture_build_identity import (
    _REPO,
    _frontend_mode,
    _git_sha,
    _verify_capturing_checkout_at,
    _verify_frontend_build_version,
)
from scripts.perf.capture_kpi_ledger import CaptureMeta, build_row, session_meta
from scripts.perf.capture_ledger import append_ledger_rows

_FRONTEND_ROOT = _REPO / "apps" / "webui" / "frontend"
_BROWSER_SCRIPT = _REPO / "scripts" / "perf" / "mode_ratio_browser.mjs"
_MIN_SAMPLE_S = 60
_PROBE_INTERVAL_S = 15
_BROWSER_EXIT_TIMEOUT_S = 30
_BROWSER_SERVICE_ID = "com.af.music-dj-tools.mode-ratio-browser"


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise SystemExit(
            "capture_mode_ratios refuses non-macOS capture: run on the reference Mac "
            "with opendj_performance_probe installed"
        )


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

    Footprint is read via `DarwinProcessMetrics.read(pid).phys_footprint`,
    the same `proc_pid_rusage` counter Activity Monitor shows and the
    packaged app's own diagnostics probe uses -- NOT `psutil`'s
    `memory_info().rss`. Summing RSS across a multi-process Chromium tree
    double-counts pages the processes share (GPU shared memory, sandboxed
    IPC buffers, mapped V8 snapshot data), so a KPI card claiming
    "physical_footprint_mb" while actually summing RSS could pass or fail
    the PERFMODE-15 threshold on an artifact of that overcounting rather
    than a real mode difference (Codex review, PR #3676).
    """

    def __init__(self, root_pid: int, *, native: DarwinProcessMetrics | None = None) -> None:
        self._root_pid = root_pid
        self._tracked: dict[int, psutil.Process] = {}
        # Lazy real construction (ctypes, Darwin-only) so tests can inject a
        # duck-typed fake and stay runnable off-macOS, matching this repo's
        # existing DarwinProcessMetrics test convention (test_probe_process_
        # family.py's _FakeNative).
        self._native = native if native is not None else DarwinProcessMetrics()

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
            if proc.pid == self._root_pid:
                # `root` is the Node `mode_ratio_browser.mjs` launcher that
                # SPAWNS Chromium via Playwright, not a member of the
                # Chromium browser/renderer family this KPI claims to
                # measure. Its own fixed footprint and CPU would dilute both
                # savings ratios with a cost that barely moves between modes
                # (Sol review, PR #3676) -- it is walked for tree discovery
                # (`_live_tree`) but excluded from the sample itself.
                continue
            try:
                footprint_mb += self._native.read(proc.pid).phys_footprint / (1024 * 1024)
                cpu_percent += proc.cpu_percent(interval=None)
                live += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
                continue
        if live == 0:
            raise RuntimeError(
                f"mode_ratio_browser process tree rooted at {self._root_pid} "
                "has no live Chromium descendant to sample (only the launcher itself is running)"
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
    print(
        f"leak samples={len(footprints)} first_mb={footprints[0]:.1f} "
        f"min_mb={min(footprints):.1f} max_mb={max(footprints):.1f} last_mb={footprints[-1]:.1f}",
        file=sys.stderr,
    )
    if slope_per_hour is None:
        raise RuntimeError("leak capture produced no computable slope")
    return slope_per_hour / 6.0


_GIG_DECKS = 4


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


def _validate_gig_stable_ids(ids: object) -> list[str]:
    """Exactly `_GIG_DECKS` non-empty string ids, or a RuntimeError naming the defect."""
    if not isinstance(ids, list) or len(ids) != _GIG_DECKS:
        raise RuntimeError(f"GIG_STABLE_IDS must hold exactly {_GIG_DECKS} ids, got {ids!r}")
    if not all(isinstance(stable_id, str) and stable_id for stable_id in ids):
        raise RuntimeError(f"GIG_STABLE_IDS holds a non-string or empty id: {ids!r}")
    return ids


def _read_browser_line(proc: subprocess.Popen[str], expected: str) -> None:
    if proc.stdout is None:
        raise RuntimeError("mode_ratio_browser stdout is not piped")
    line = proc.stdout.readline().strip()
    if line != expected:
        raise RuntimeError(
            f"mode_ratio_browser expected {expected!r}, got {line!r}; "
            f"helper stderr: {_browser_stderr_tail(proc)}"
        )


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
    if proc.stdin is None:
        raise RuntimeError("mode_ratio_browser stdin is not piped")
    proc.stdin.write("NEXT\n")
    proc.stdin.flush()


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
) -> tuple[dict[str, float], dict[str, float], list[str]]:
    proc = _start_browser_session(frontend, "gig-trackify")
    try:
        gig_stable_ids = _read_gig_stable_ids(proc)
        _read_browser_line(proc, "GIG_READY")
        gig = _sample_steady(proc.pid, duration_s)
        _signal_browser(proc)
        _read_browser_line(proc, "TRACKIFY_READY")
        trackify = _sample_steady(proc.pid, duration_s)
        _signal_browser(proc)
        _finish_browser_session(proc)
        return gig, trackify, gig_stable_ids
    finally:
        if proc.poll() is None:
            proc.kill()


def _capture_trackify_leak(frontend: str, duration_s: int) -> float:
    proc = _start_browser_session(frontend, "trackify-leak")
    try:
        _read_browser_line(proc, "TRACKIFY_READY")
        slope = _sample_leak(proc.pid, duration_s)
        _signal_browser(proc)
        _finish_browser_session(proc)
        return slope
    finally:
        if proc.poll() is None:
            proc.kill()


_METHOD = (
    "process-tree physical-footprint/CPU sampling of the Playwright-launched Chromium running "
    "Gig/Trackify (PERFMODE-15) -- browser/renderer process family only, not the "
    "packaged app's telemetry endpoint and not the python engine (no per-process "
    "CPU is exposed for that family by any existing endpoint). This is a NARROWER "
    "scope than capture_library_mode.py's PERFMODE-14 ratios, which also attribute "
    "the engine process and its stem-worker descendants: the two ratio families "
    "exclude different processes and are not directly comparable cross-KPI."
)


def _capture_identity_reason(
    frontend: str, expected_sha: str, repo_root: Path = _REPO
) -> str | None:
    """None when the checkout at `repo_root` is clean at `expected_sha` and
    `frontend` serves that same static build; otherwise why not.

    `main()` runs this before the capture and again before any row is written
    (Sol P1/BLOCKING, PR #4540): the capture can run for an hour, and a
    checkout that moved or went dirty, or a frontend redeployed mid-run, would
    otherwise mix builds under one clean-looking sha. The checkout is checked
    first so a dirty tree refuses without touching the network.
    """
    checkout_reason = _verify_capturing_checkout_at(expected_sha, repo_root)
    if checkout_reason is not None:
        return checkout_reason
    if _frontend_mode(frontend) == "vite-dev":
        return (
            "capture_mode_ratios refuses a vite-dev frontend: /_app/version.json 404s in "
            "dev mode, so it cannot confirm its own build identity (mirrors "
            "capture_library_targets.py's vite-dev refusal, PR #4034, discussion_r4132371694)"
        )
    return _verify_frontend_build_version(frontend, expected_sha)


def _gig_baseline_rows(
    gig: dict[str, float],
    trackify: dict[str, float],
    gig_stable_ids: object,
    meta: CaptureMeta,
) -> list[dict[str, Any]]:
    """The footprint and CPU ratio rows for one Gig-then-Trackify capture.

    Refuses a capture that cannot name its four Gig decks or has a zero
    denominator: neither is an auditable measurement.
    """
    stable_ids = _validate_gig_stable_ids(gig_stable_ids)
    if gig["footprint_mb"] <= 0 or gig["cpu_percent"] <= 0:
        raise SystemExit("gig baseline denominators missing or zero; refusing ratio write")
    footprint_ratio = 1.0 - (trackify["footprint_mb"] / gig["footprint_mb"])
    cpu_ratio = 1.0 - (trackify["cpu_percent"] / gig["cpu_percent"])
    note = (
        f"PERFMODE-15 trackify ratios; gig_fp={gig['footprint_mb']:.2f}MB "
        f"trackify_fp={trackify['footprint_mb']:.2f}MB "
        f"gig_cpu={gig['cpu_percent']:.2f}% trackify_cpu={trackify['cpu_percent']:.2f}% "
        f"samples_gig={int(gig['sample_count'])} "
        f"samples_trackify={int(trackify['sample_count'])} "
        f"gig_stable_ids={stable_ids!r}"
    )
    return [
        build_row(
            kpi=kpi,
            value=round(value, 4),
            unit="ratio",
            method=_METHOD,
            meta=meta,
            note=note,
            measured=True,
        )
        for kpi, value in (
            ("trackify_mode_footprint_ratio", footprint_ratio),
            ("trackify_mode_cpu_ratio", cpu_ratio),
        )
    ]


def _append_rows_after_reverification(
    ledger: Path, rows: list[dict[str, Any]], frontend: str, sha: str, repo_root: Path = _REPO
) -> None:
    """Re-run every identity gate after sampling, then append; never append on a refusal.

    Sol P1/BLOCKING (PR #4034, discussion_r4149791234): the leak capture can
    run for an hour, so every identity gate runs again after sampling and
    before any row is appended, as capture_library_mode.py does.
    """
    post_reason = _capture_identity_reason(frontend, sha, repo_root)
    if post_reason is not None:
        raise SystemExit(
            "refusing to write rows: post-capture reverification failed (checkout or "
            f"frontend changed during the capture): {post_reason}"
        )
    append_ledger_rows(ledger, rows)


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

    meta = session_meta(sha=sha)
    rows: list[dict[str, Any]] = []

    if args.gig_baseline:
        gig, trackify, gig_stable_ids = _capture_gig_then_trackify(args.frontend, args.duration_s)
        rows.extend(_gig_baseline_rows(gig, trackify, gig_stable_ids, meta))

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

    _append_rows_after_reverification(args.ledger, rows, args.frontend, sha)
    print(json.dumps({"capture_id": meta.capture_id, "rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
