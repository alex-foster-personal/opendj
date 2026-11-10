"""Measure main-isolate JS heap after the All Tracks pane fills on /performance boot.

Issue #10232 (epic #9376, #5597). Requirement PERF-CAPTURE-10797.

Builds a synthetic library, boots the engine, runs a Chromium-only Playwright spec that
waits for perf ring ``library-load-all-tracks``, then reads heap via CDP:
``HeapProfiler.collectGarbage`` twice, ``Runtime.getHeapUsage`` (MiB). Main isolate only;
not the app's ``performance.memory`` js_heap_mb telemetry.

Usage::

    uv run python -m scripts.perf.js_heap_after_pane --tracks 10000 --json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from apps.shared.process_tree import KILL_CONFIRM_S, root_of, spawn_tree, stop_tree
from scripts.perf.capture_kpi_ledger import (
    DEFAULT_LEDGER,
    CaptureMeta,
    append_entries,
    build_row,
    format_appended,
    git_sha,
    session_meta,
    withheld_row,
)
from scripts.perf.generate_library_scale_fixture import build as build_fixture
from scripts.perf.playwright_capture import run_playwright_capture

KPI = "js_heap_mb_after_pane"
UNIT = "MB"
RING_KIND = "library-load-all-tracks"
DEFAULT_TRACKS = 10_000
DEFAULT_SEED = 1
DEFAULT_TIMEOUT_S = 1200
ENGINE_READY_TIMEOUT_S = 120.0
ENGINE_STOP_GRACE_S = 5.0
MIB = 1024 * 1024
METHOD = (
    "Chromium page CDP after the /performance boot All Tracks fill "
    f"(perf ring {RING_KIND}): HeapProfiler.collectGarbage x2 then "
    "Runtime.getHeapUsage usedSize, MiB; main isolate only; not the app's "
    "performance.memory js_heap_mb"
)
_REPO = Path(__file__).resolve().parents[2]
_FRONTEND_ROOT = _REPO / "apps" / "webui" / "frontend"
_BUILD_INDEX = _FRONTEND_ROOT / "build" / "index.html"
_PLAYWRIGHT_CONFIG = "tests/e2e/playwright.js-heap-after-pane.config.ts"


def _reading(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _positive_int_rows(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0:
        return None
    return value


def reduce_heap_readings(
    after_pane_bytes: object,
    idle_bytes: object,
    rows: object,
) -> dict[str, Any]:
    """Pure reducer from CDP byte readings and row count to reported MB fields."""
    after = _reading(after_pane_bytes)
    idle = _reading(idle_bytes)
    row_count = _positive_int_rows(rows)

    base: dict[str, Any] = {
        "js_heap_mb_after_pane": None,
        "js_heap_mb_idle_delta": None,
        "rows": row_count,
        "mb_per_1k_rows": None,
        "withheld_reason": None,
    }

    if after is None:
        return {**base, "ok": False, "withheld_reason": "missing after-pane heap reading"}
    if idle is None:
        return {**base, "ok": False, "withheld_reason": "missing idle heap reading"}
    if row_count is None:
        return {
            **base,
            "ok": False,
            "rows": None,
            "withheld_reason": f"perf ring row reported no positive row count: {rows!r}",
        }

    after_mb = after / MIB
    idle_mb = idle / MIB
    return {
        "ok": True,
        "js_heap_mb_after_pane": round(after_mb, 1),
        "js_heap_mb_idle_delta": round(idle_mb - after_mb, 1),
        "rows": row_count,
        "mb_per_1k_rows": round(after_mb / (row_count / 1000), 1),
        "withheld_reason": None,
    }


def report_from_capture(
    result: dict[str, Any],
    *,
    tracks: int,
    seed: int,
) -> dict[str, Any]:
    if result.get("ok") is not True:
        reason = result.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reason = "heap capture failed without a reason"
        withheld = reduce_heap_readings(None, None, None)
        withheld["ok"] = False
        withheld["withheld_reason"] = reason
        withheld["js_heap_mb_after_pane"] = None
        withheld["js_heap_mb_idle_delta"] = None
        withheld["rows"] = None
        withheld["mb_per_1k_rows"] = None
        return {
            **withheld,
            "tracks": tracks,
            "seed": seed,
            "kpi": KPI,
            "method": METHOD,
        }

    reduced = reduce_heap_readings(
        result.get("after_pane_used_bytes"),
        result.get("idle_used_bytes"),
        result.get("rows"),
    )
    return {
        **reduced,
        "tracks": tracks,
        "seed": seed,
        "kpi": KPI,
        "method": METHOD,
    }


def withheld_report(reason: str, *, tracks: int, seed: int) -> dict[str, Any]:
    return report_from_capture({"ok": False, "reason": reason}, tracks=tracks, seed=seed)


def ledger_row(report: dict[str, Any], meta: CaptureMeta) -> dict[str, Any]:
    tracks = report.get("tracks", DEFAULT_TRACKS)
    seed = report.get("seed", DEFAULT_SEED)
    if report.get("ok") is True:
        return build_row(
            kpi=KPI,
            value=report["js_heap_mb_after_pane"],
            unit=UNIT,
            method=METHOD,
            meta=meta,
            note=(
                f"rows={report.get('rows')}; mb_per_1k_rows={report.get('mb_per_1k_rows')}; "
                f"js_heap_mb_idle_delta={report.get('js_heap_mb_idle_delta')}; "
                f"tracks={tracks}; seed={seed}"
            ),
        )
    reason = report.get("withheld_reason") or "withheld"
    return withheld_row(
        kpi=KPI,
        unit=UNIT,
        method=METHOD,
        meta=meta,
        reason=f"{reason}; tracks={tracks}; seed={seed}",
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _health_ok(base_url: str, timeout_s: float = 2.0) -> bool:
    url = f"{base_url.rstrip('/')}/api/v1/health"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


@contextlib.contextmanager
def _fixture_engine(tracks: int, seed: int) -> Iterator[tuple[str | None, str | None]]:
    data_dir = Path(tempfile.mkdtemp(prefix="mdt-js-heap-")).resolve()
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    proc: subprocess.Popen[bytes] | None = None
    log_path = data_dir / "engine.log"
    reason: str | None = None
    yielded_url: str | None = None
    try:
        build_fixture(data_dir, tracks=tracks, seed=seed)
        sandbox_home = data_dir / "sandbox-home"
        sandbox_home.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["MDT_DATA_DIR"] = str(data_dir)
        env["MDT_LIBRARY_MODE"] = "local"
        env["HOME"] = str(sandbox_home)
        env["USERPROFILE"] = str(sandbox_home)
        env["WEB_CONCURRENCY"] = ""
        argv = [
            sys.executable,
            "-m",
            "apps.engine_core",
            "serve",
            "--data-dir",
            str(data_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        with log_path.open("ab") as log_handle:
            proc, _root = spawn_tree(
                argv,
                cwd=_REPO,
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + ENGINE_READY_TIMEOUT_S
        while True:
            if proc.poll() is not None:
                reason = f"engine exited before healthy (code={proc.returncode})"
                break
            if _health_ok(url):
                yielded_url = url
                break
            if time.monotonic() >= deadline:
                reason = "engine not healthy within 120s"
                break
            time.sleep(0.25)
        yield yielded_url, reason
    finally:
        if proc is not None and proc.poll() is None:
            stop_tree(root_of(proc), grace_s=ENGINE_STOP_GRACE_S + KILL_CONFIRM_S)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=ENGINE_STOP_GRACE_S)
        shutil.rmtree(data_dir, ignore_errors=True)


def capture(*, tracks: int, seed: int, timeout_s: int) -> dict[str, Any]:
    if not _BUILD_INDEX.is_file():
        return withheld_report(
            "frontend production build missing: apps/webui/frontend/build/index.html; run pnpm build",
            tracks=tracks,
            seed=seed,
        )
    try:
        with _fixture_engine(tracks, seed) as (url, reason):
            if url is None:
                return withheld_report(reason or "engine unavailable", tracks=tracks, seed=seed)
            result = run_playwright_capture(
                engine=url,
                frontend=url,
                frontend_root=_FRONTEND_ROOT,
                playwright_config=_PLAYWRIGHT_CONFIG,
                timeout_s=timeout_s,
            )
            return report_from_capture(result, tracks=tracks, seed=seed)
    except RuntimeError as exc:
        return withheld_report(str(exc), tracks=tracks, seed=seed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JS heap after All Tracks pane fill (Chromium CDP).")
    parser.add_argument("--tracks", type=int, default=DEFAULT_TRACKS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--ledger",
        nargs="?",
        type=Path,
        const=DEFAULT_LEDGER,
        default=None,
    )
    args = parser.parse_args(argv)
    if args.tracks <= 0:
        parser.error("--tracks must be positive")
    if args.timeout_s <= 0 or args.timeout_s > 3600:
        parser.error("--timeout-s must be between 1 and 3600")

    report = capture(tracks=args.tracks, seed=args.seed, timeout_s=args.timeout_s)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif report.get("ok"):
        print(
            f"js_heap_mb_after_pane={report['js_heap_mb_after_pane']} MB "
            f"rows={report['rows']} "
            f"mb_per_1k_rows={report['mb_per_1k_rows']} "
            f"js_heap_mb_idle_delta={report['js_heap_mb_idle_delta']}"
        )
    else:
        print(f"withheld: {report.get('withheld_reason')}")

    if args.ledger is not None:
        row = ledger_row(report, session_meta(sha=git_sha()))
        append_entries(args.ledger, [row])
        print(format_appended(row), file=sys.stderr)
        print(
            f"[js-heap-after-pane] ledger row: kpi={row['kpi']} "
            f"capture_id={row.get('capture_id', '')} path={args.ledger}",
            file=sys.stderr,
        )

    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
