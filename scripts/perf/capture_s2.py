"""Drive real play/pause presses in Playwright and capture S2 press-to-audible p99."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from scripts.perf.capture_kpi_ledger import (
    append_entries,
    format_appended,
    git_sha,
    session_meta,
)
from scripts.perf.capture_s2_agg import presses_to_ledger_rows, withheld_rows

_REPO = Path(__file__).resolve().parents[2]
_FRONTEND_ROOT = _REPO / "apps" / "webui" / "frontend"
_PLAYWRIGHT_CONFIG = "tests/e2e/playwright.kpi-s2-capture.config.ts"


def _run_playwright_capture(
    *,
    engine: str,
    frontend: str,
    presses: int,
    browser: str,
    track: str | None,
    timeout_s: int,
) -> dict[str, Any] | None:
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
    env["KPI_CAPTURE_PRESSES"] = str(presses)
    env["KPI_CAPTURE_BROWSER"] = browser
    env["KPI_CAPTURE_TIMEOUT_S"] = str(timeout_s)
    if track:
        env["KPI_CAPTURE_TRACK"] = track
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
        result = json.loads(raw) if raw.strip() else None
    except (OSError, json.JSONDecodeError):
        result = None
    finally:
        Path(result_path).unlink(missing_ok=True)
    if result is None:
        detail = proc.stderr.strip() or proc.stdout.strip()
        return {
            "ok": False,
            "reason": (
                "playwright capture did not write KPI_CAPTURE_RESULT "
                f"(exit {proc.returncode})"
                + (f": {detail}" if detail else "")
            ),
        }
    if proc.returncode != 0 and result.get("ok") is not True:
        result.setdefault("ok", False)
        result.setdefault(
            "reason",
            f"playwright exited {proc.returncode}",
        )
    return result


def _finish(ledger: Path, rows: list[dict[str, Any]], dry_run: bool) -> None:
    if rows and not dry_run:
        append_entries(ledger, rows)
    for row in rows:
        print(format_appended(row))


def capture_s2(
    *,
    engine: str,
    frontend: str,
    ledger_path: Path,
    presses: int = 32,
    browser: str = "webkit",
    timeout_s: int = 90,
    track: str | None = None,
    dry_run: bool = False,
) -> int:
    """Capture S2 press-to-audible p99 and append ledger rows."""
    from scripts.perf.capture_kpis import probe_health

    sha = git_sha()
    meta = session_meta(sha=sha)
    if not sha:
        rows = withheld_rows("git sha cannot be read", meta)
        _finish(ledger_path, rows, dry_run)
        return 1

    _health, health_err = probe_health(engine.rstrip("/"))
    if health_err is not None:
        rows = withheld_rows(health_err, meta)
        _finish(ledger_path, rows, dry_run)
        return 1

    result = _run_playwright_capture(
        engine=engine.rstrip("/"),
        frontend=frontend.rstrip("/"),
        presses=presses,
        browser=browser,
        track=track,
        timeout_s=timeout_s,
    )
    rows = presses_to_ledger_rows(result, meta, requested_presses=presses)
    _finish(ledger_path, rows, dry_run)
    audible = next((row for row in rows if row.get("kpi") == "input_to_audible_ms_p99"), None)
    if audible is None:
        return 1
    value = audible.get("value")
    if audible.get("status") == "withheld" or value is None:
        return 1
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return 0
    return 1
