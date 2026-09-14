"""Drive BOOT-LIB capture in Playwright and append open_to_library_rows_ms to the ledger.

Uses the same perf-capture toolchain as S13 but a dedicated boot-library spec
(issue #2697, PERF-UI-03).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scripts.perf.capture_boot_library_ledger import (
    classify_boot_library_withhold_reason,
    span_to_ledger_rows,
)
from scripts.perf.capture_kpi_ledger import format_appended
from scripts.perf.capture_ledger import append_ledger_rows

_REPO = Path(__file__).resolve().parents[2]
_FRONTEND_ROOT = _REPO / "apps" / "webui" / "frontend"
_PLAYWRIGHT_CONFIG = "tests/e2e/playwright.kpi-boot-library-capture.config.ts"
_PROBE_TIMEOUT_S = 10.0


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


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _http_json(
    method: str,
    url: str,
    timeout_s: float = _PROBE_TIMEOUT_S,
) -> tuple[int, Any]:
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


def _run_playwright_capture(
    *,
    engine: str,
    frontend: str,
    timeout_s: int,
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
    env["PERFORMANCE_E2E_BASE_URL"] = frontend.rstrip("/")
    env["PERFORMANCE_E2E_API_BASE"] = engine.rstrip("/")
    env["KPI_CAPTURE_ENGINE"] = engine.rstrip("/")
    env["KPI_CAPTURE_FRONTEND"] = frontend.rstrip("/")
    env["KPI_CAPTURE_RESULT"] = result_path
    env["KPI_CAPTURE_TIMEOUT_S"] = str(timeout_s)
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
            "span": None,
            "reason": (
                "playwright capture did not write KPI_CAPTURE_RESULT "
                f"(exit {proc.returncode})"
            ),
        }
    finally:
        Path(result_path).unlink(missing_ok=True)
    if proc.returncode != 0 and not result.get("reason"):
        result.setdefault("ok", False)
        result.setdefault("span", None)
        result["reason"] = (
            result.get("reason")
            or f"playwright exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return result


def _emit_withheld_stderr(rows: list[dict[str, Any]], ledger_path: Path) -> None:
    scored = next(row for row in rows if row.get("kpi") == "open_to_library_rows_ms")
    print(format_appended(scored), file=sys.stderr)
    print(
        "[capture-boot-library] ledger row: "
        f"kpi={scored['kpi']} date={scored['date']} "
        f"capture_id={scored.get('capture_id', '')} path={ledger_path}",
        file=sys.stderr,
    )


def capture_boot_library(
    *,
    engine: str,
    frontend: str,
    ledger_path: Path,
    timeout_s: int = 90,
    probe_timeout_s: float = _PROBE_TIMEOUT_S,
    dry_run: bool = False,
) -> int:
    """Capture BOOT-LIB KPI and append ledger rows. Returns process exit code."""
    sha = _git_sha()
    machine = _machine_name()
    capture_date = _utc_today()

    probe_reason = _probe_engine(engine, timeout_s=probe_timeout_s)
    if probe_reason is not None:
        rows = span_to_ledger_rows(
            None,
            sha=sha,
            machine=machine,
            capture_date=capture_date,
            reason=probe_reason,
        )
        if not dry_run:
            append_ledger_rows(ledger_path, rows)
        _emit_withheld_stderr(rows, ledger_path)
        return 1

    result = _run_playwright_capture(
        engine=engine,
        frontend=frontend,
        timeout_s=timeout_s,
    )
    span = result.get("span") if isinstance(result.get("span"), dict) else None
    reason = result.get("reason") if isinstance(result.get("reason"), str) else None
    if result.get("ok") is not True:
        rows = span_to_ledger_rows(
            span,
            sha=sha,
            machine=machine,
            capture_date=capture_date,
            reason=classify_boot_library_withhold_reason(reason),
        )
        if not dry_run:
            append_ledger_rows(ledger_path, rows)
        _emit_withheld_stderr(rows, ledger_path)
        return 1

    rows = span_to_ledger_rows(
        span,
        sha=sha,
        machine=machine,
        capture_date=capture_date,
    )
    if dry_run:
        print(json.dumps(rows, indent=2))
        return 0
    append_ledger_rows(ledger_path, rows)
    return 0
