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
import math
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import IO, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scripts.diagnostics.probe_log_store import _linear_slope_mb_per_hour
from scripts.perf.capture_kpi_ledger import build_row, session_meta
from scripts.perf.capture_ledger import append_ledger_rows

_REPO = Path(__file__).resolve().parents[2]
_FRONTEND_ROOT = _REPO / "apps" / "webui" / "frontend"
_BROWSER_SCRIPT = _REPO / "scripts" / "perf" / "mode_ratio_browser.mjs"
_MIN_SAMPLE_S = 60
_PROBE_INTERVAL_S = 15
_PROBE_TIMEOUT_S = 10.0
_TELEMETRY_PATH = "/api/v1/performance/telemetry/processes"
_ROLE_FALLBACK = ("desktop-shell", "python-engine", "webkit-webcontent")
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


def _telemetry_url(frontend: str) -> str:
    return f"{frontend.rstrip('/')}{_TELEMETRY_PATH}"


def _dict_field(body: dict[str, Any], key: str) -> dict[str, Any]:
    """Narrows `body[key]` to a dict, binding the isinstance-checked value
    itself (rather than re-calling `.get()` in the branch) so mypy can
    actually narrow it instead of inferring `dict | Any | None`."""
    value = body.get(key)
    return value if isinstance(value, dict) else {}


def _footprint_mb_from_telemetry(body: dict[str, Any]) -> float:
    totals = _dict_field(body, "totals")
    footprint = totals.get("physical_footprint_mb")
    if isinstance(footprint, (int, float)) and math.isfinite(float(footprint)):
        return float(footprint)
    by_role = _dict_field(body, "by_role_mb")
    summed = 0.0
    for role in _ROLE_FALLBACK:
        value = by_role.get(role)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            summed += float(value)
    if summed <= 0:
        raise RuntimeError("telemetry response missing physical_footprint_mb totals")
    return summed


def _cpu_percent_from_telemetry(body: dict[str, Any]) -> float:
    totals = _dict_field(body, "totals")
    cpu = totals.get("cpu_percent")
    if isinstance(cpu, (int, float)) and math.isfinite(float(cpu)):
        return float(cpu)
    raise RuntimeError("telemetry response missing cpu_percent totals")


def _probe_once(frontend: str) -> dict[str, Any]:
    url = _telemetry_url(frontend)
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=_PROBE_TIMEOUT_S) as response:
            status = response.status
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        raise RuntimeError(f"telemetry probe failed ({exc.code}) for {url}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"telemetry probe unreachable for {url}: {exc}") from exc
    if status != 200:
        raise RuntimeError(f"telemetry probe failed ({status}) for {url}")
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise RuntimeError(  # noqa: TRY004 -- one exception type for every probe failure
            f"telemetry probe returned non-object JSON for {url}"
        )
    if body.get("available") is not True:
        reason = body.get("reason", "telemetry unavailable")
        raise RuntimeError(f"telemetry unavailable for {url}: {reason}")
    return {
        "totals": {
            "physical_footprint_mb": _footprint_mb_from_telemetry(body),
            "cpu_percent": _cpu_percent_from_telemetry(body),
        }
    }


def _sample_steady(frontend: str, duration_s: int) -> dict[str, float]:
    if duration_s < _MIN_SAMPLE_S:
        raise ValueError(f"duration must be at least {_MIN_SAMPLE_S}s, got {duration_s}")
    footprints: list[float] = []
    cpus: list[float] = []
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        sample = _probe_once(frontend)
        totals = _dict_field(sample, "totals")
        fp = totals.get("physical_footprint_mb")
        cpu = totals.get("cpu_percent")
        if isinstance(fp, (int, float)):
            footprints.append(float(fp))
        if isinstance(cpu, (int, float)):
            cpus.append(float(cpu))
        time.sleep(_PROBE_INTERVAL_S)
    if not footprints or not cpus:
        raise RuntimeError("probe returned no footprint or cpu samples")
    return {
        "footprint_mb": sum(footprints) / len(footprints),
        "cpu_percent": sum(cpus) / len(cpus),
        "sample_count": float(len(footprints)),
    }


def _sample_leak(frontend: str, duration_s: int) -> float:
    elapsed: list[float] = []
    footprints: list[float] = []
    start = time.monotonic()
    deadline = start + duration_s
    while time.monotonic() < deadline:
        sample = _probe_once(frontend)
        totals = _dict_field(sample, "totals")
        fp = totals.get("physical_footprint_mb")
        if isinstance(fp, (int, float)):
            elapsed.append(time.monotonic() - start)
            footprints.append(float(fp))
        time.sleep(_PROBE_INTERVAL_S)
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
        gig = _sample_steady(frontend, duration_s)
        _signal_browser(proc)
        _read_browser_line(proc.stdout, "TRACKIFY_READY")
        trackify = _sample_steady(frontend, duration_s)
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
        slope = _sample_leak(frontend, duration_s)
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


_METHOD = "engine telemetry /performance/telemetry/processes steady-state sampling (PERFMODE-15)"


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
