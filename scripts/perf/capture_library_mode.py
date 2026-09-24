"""Capture Gig vs Library steady-state footprint and CPU ratios (PERFMODE-14).

Reference Mac only for scored ledger rows. Linux exits 69 UNAVAILABLE before sampling.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scripts.perf.capture_kpi_ledger import append_entries, format_appended

_REPO = Path(__file__).resolve().parents[2]
_FRONTEND_ROOT = _REPO / "apps" / "webui" / "frontend"
_PLAYWRIGHT_CONFIG = "tests/e2e/playwright.library-mode-perf.config.ts"
_EXIT_UNAVAILABLE = 69
_PROBE_TIMEOUT_S = 10.0
_DEFAULT_CAPTURE_TIMEOUT_S = 300


def _git_sha() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _machine_name() -> str:
    return socket.gethostname().split(".")[0]


def _require_reference_mac(dry_run: bool) -> None:
    if dry_run:
        return
    if platform.system() != "Darwin":
        print(
            "library mode KPI capture requires the reference Mac (Darwin); "
            "Linux CI proves schema only",
            file=sys.stderr,
        )
        raise SystemExit(_EXIT_UNAVAILABLE)


def _build_capture_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"issue-2700-library-mode-{stamp}"


def _http_json(method: str, url: str, timeout_s: float = _PROBE_TIMEOUT_S) -> tuple[int, Any]:
    request = Request(url, headers={"Accept": "application/json"}, method=method)
    try:
        with urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if raw else None
            return response.status, payload
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw
        return exc.code, payload
    except URLError as exc:
        raise ConnectionError(str(exc.reason)) from exc
    except OSError as exc:
        raise ConnectionError(f"{type(exc).__name__}: {exc}") from exc


def _probe_engine(engine: str, timeout_s: float = _PROBE_TIMEOUT_S) -> str | None:
    try:
        status, _payload = _http_json(
            "GET", f"{engine.rstrip('/')}/api/v1/health", timeout_s=timeout_s
        )
    except ConnectionError as exc:
        return f"engine unreachable: {exc}"
    if status != 200:
        return f"engine health returned HTTP {status}"
    return None


def _ledger_rows(
    *,
    capture_id: str,
    machine: str,
    app_build_sha: str,
    gig_footprint_mb: float,
    library_footprint_mb: float,
    gig_cpu_percent: float,
    library_cpu_percent: float,
    measured: bool = True,
    note: str | None = None,
) -> list[dict[str, Any]]:
    footprint_ratio = library_footprint_mb / gig_footprint_mb
    cpu_ratio = library_cpu_percent / gig_cpu_percent
    if note is None:
        note = (
            f"capture_id={capture_id} app_build_sha={app_build_sha} "
            "method=scripts/perf/capture_library_mode.py dwell_seconds=60"
        )
    today = datetime.now(UTC).date().isoformat()
    return [
        {
            "date": today,
            "round": capture_id,
            "kpi": "library_mode_footprint_ratio",
            "unit": "ratio",
            "value": round(footprint_ratio, 4),
            "machine": machine,
            "source": "capture_library_mode",
            "note": note,
            "measured": measured,
        },
        {
            "date": today,
            "round": capture_id,
            "kpi": "library_mode_cpu_ratio",
            "unit": "ratio",
            "value": round(cpu_ratio, 4),
            "machine": machine,
            "source": "capture_library_mode",
            "note": note,
            "measured": measured,
        },
    ]


def _run_playwright_capture(
    *,
    engine: str,
    frontend: str,
    data_dir: str,
    capture_id: str,
    timeout_s: int,
    dwell_seconds: int,
) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as handle:
        result_path = handle.name
    env = os.environ.copy()
    env["PERFORMANCE_E2E_START_SERVERS"] = "0"
    env["PERFORMANCE_E2E_FIXTURE"] = "0"
    env["PERFORMANCE_E2E_BASE_URL"] = frontend.rstrip("/")
    env["PERFORMANCE_E2E_API_BASE"] = engine.rstrip("/")
    env["MDT_DATA_DIR"] = data_dir
    env["KPI_CAPTURE_RESULT"] = result_path
    env["KPI_CAPTURE_TIMEOUT_S"] = str(timeout_s)
    env["KPI_CAPTURE_ID"] = capture_id
    env["KPI_CAPTURE_DWELL_SECONDS"] = str(dwell_seconds)
    proc = subprocess.run(
        [
            "pnpm",
            "exec",
            "playwright",
            "test",
            "--config",
            _PLAYWRIGHT_CONFIG,
        ],
        cwd=_FRONTEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    try:
        raw = Path(result_path).read_text(encoding="utf-8")
        result = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        result = {
            "ok": False,
            "reason": (
                "playwright capture did not write KPI_CAPTURE_RESULT "
                f"(exit {proc.returncode})"
            ),
        }
    finally:
        Path(result_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        result["ok"] = False
        result["reason"] = (
            result.get("reason")
            or f"playwright exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return result


def _rows_from_capture_result(
    result: dict[str, Any],
    *,
    capture_id: str,
    machine: str,
    app_build_sha: str,
    dwell_seconds: int,
) -> list[dict[str, Any]]:
    gig = result.get("gig") if isinstance(result.get("gig"), dict) else None
    library = result.get("library") if isinstance(result.get("library"), dict) else None
    if gig is None or library is None:
        raise ValueError(result.get("reason") or "capture result missing gig/library medians")
    sampling_method = result.get("sampling_method")
    method_note = (
        f"sampling_method={sampling_method}" if isinstance(sampling_method, str) else "method=scripts/perf/capture_library_mode.py"
    )
    return _ledger_rows(
        capture_id=capture_id,
        machine=machine,
        app_build_sha=app_build_sha,
        gig_footprint_mb=float(gig["median_footprint_mb"]),
        library_footprint_mb=float(library["median_footprint_mb"]),
        gig_cpu_percent=float(gig["median_cpu_percent"]),
        library_cpu_percent=float(library["median_cpu_percent"]),
        note=f"capture_id={capture_id} app_build_sha={app_build_sha} dwell_seconds={dwell_seconds} {method_note}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture PERFMODE-14 library mode KPI ratios")
    parser.add_argument("--engine", help="Backend base URL (alias: --api-base)")
    parser.add_argument("--frontend", help="Frontend base URL (alias: --base-url)")
    parser.add_argument("--api-base", help="Backend base URL")
    parser.add_argument("--base-url", help="Frontend base URL")
    parser.add_argument("--data-dir", required=True, help="Real library data directory")
    parser.add_argument("--dwell-seconds", type=int, default=60)
    parser.add_argument("--timeout-s", type=int, default=_DEFAULT_CAPTURE_TIMEOUT_S)
    parser.add_argument(
        "--ledger",
        type=Path,
        default=_REPO / "docs" / "perf" / "kpi-ledger.json",
    )
    parser.add_argument("--machine-tag", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--gig-footprint-mb", type=float, default=None)
    parser.add_argument("--library-footprint-mb", type=float, default=None)
    parser.add_argument("--gig-cpu-percent", type=float, default=None)
    parser.add_argument("--library-cpu-percent", type=float, default=None)
    args = parser.parse_args(argv)

    if args.dry_run:
        _require_reference_mac(True)
        capture_id = _build_capture_id()
        machine = args.machine_tag or _machine_name()
        rows = _ledger_rows(
            capture_id=capture_id,
            machine=machine,
            app_build_sha=_git_sha(),
            gig_footprint_mb=1000.0,
            library_footprint_mb=500.0,
            gig_cpu_percent=10.0,
            library_cpu_percent=3.0,
            measured=False,
            note=(
                f"capture_id={capture_id} app_build_sha={_git_sha()} "
                "method=scripts/perf/capture_library_mode.py --dry-run "
                "(hardcoded placeholder values, not a capture)"
            ),
        )
        print(json.dumps({"capture_id": capture_id, "rows": rows}, indent=2))
        return 0

    _require_reference_mac(False)

    engine = args.engine or args.api_base
    frontend = args.frontend or args.base_url
    if engine is None or frontend is None:
        parser.error("--engine and --frontend are required (or --api-base and --base-url)")

    manual = (
        args.gig_footprint_mb,
        args.library_footprint_mb,
        args.gig_cpu_percent,
        args.library_cpu_percent,
    )
    capture_id = _build_capture_id()
    machine = args.machine_tag or _machine_name()
    app_build_sha = _git_sha()

    if all(value is not None for value in manual):
        rows = _ledger_rows(
            capture_id=capture_id,
            machine=machine,
            app_build_sha=app_build_sha,
            gig_footprint_mb=float(args.gig_footprint_mb),
            library_footprint_mb=float(args.library_footprint_mb),
            gig_cpu_percent=float(args.gig_cpu_percent),
            library_cpu_percent=float(args.library_cpu_percent),
            measured=False,
            note=(
                f"capture_id={capture_id} app_build_sha={app_build_sha} "
                "method=scripts/perf/capture_library_mode.py "
                "values supplied by hand on the command line (not a dwell capture)"
            ),
        )
        append_entries(args.ledger, rows)
        for row in rows:
            print(format_appended(row))
        return 0

    probe_reason = _probe_engine(engine)
    if probe_reason is not None:
        print(probe_reason, file=sys.stderr)
        return 1

    result = _run_playwright_capture(
        engine=engine.rstrip("/"),
        frontend=frontend.rstrip("/"),
        data_dir=args.data_dir,
        capture_id=capture_id,
        timeout_s=args.timeout_s,
        dwell_seconds=args.dwell_seconds,
    )
    if result.get("ok") is not True:
        print(result.get("reason") or "library mode capture failed", file=sys.stderr)
        return 1

    try:
        rows = _rows_from_capture_result(
            result,
            capture_id=capture_id,
            machine=machine,
            app_build_sha=app_build_sha,
            dwell_seconds=args.dwell_seconds,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    append_entries(args.ledger, rows)
    for row in rows:
        print(format_appended(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
