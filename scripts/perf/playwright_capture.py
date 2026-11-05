"""Run a Playwright KPI capture and normalize its result-file contract."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def run_playwright_capture(
    *,
    engine: str,
    frontend: str,
    frontend_root: Path,
    playwright_config: str,
    timeout_s: int,
    google_storage_state: str | None = None,
) -> dict[str, Any]:
    """Run one KPI capture with the shared Playwright result-file protocol."""
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise RuntimeError("pnpm executable was not found on PATH")

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as handle:
        result_path = Path(handle.name)

    env = os.environ.copy()
    env["PERFORMANCE_E2E_START_SERVERS"] = "0"
    env["PERFORMANCE_E2E_BASE_URL"] = frontend.rstrip("/")
    env["PERFORMANCE_E2E_API_BASE"] = engine.rstrip("/")
    env["KPI_CAPTURE_ENGINE"] = engine.rstrip("/")
    env["KPI_CAPTURE_FRONTEND"] = frontend.rstrip("/")
    env["KPI_CAPTURE_RESULT"] = str(result_path)
    env["KPI_CAPTURE_TIMEOUT_S"] = str(timeout_s)
    if google_storage_state:
        env["KPI_CAPTURE_GOOGLE_STORAGE_STATE"] = google_storage_state

    proc = subprocess.run(
        [
            pnpm,
            "exec",
            "playwright",
            "test",
            "--config",
            playwright_config,
        ],
        cwd=frontend_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        raw = result_path.read_text(encoding="utf-8")
        parsed = json.loads(raw) if raw.strip() else {}
        if not isinstance(parsed, dict):
            raise TypeError("Playwright KPI capture result must be a JSON object")
        result: dict[str, Any] = parsed
    except (OSError, json.JSONDecodeError):
        result = {
            "ok": False,
            "span": None,
            "reason": (f"playwright capture did not write KPI_CAPTURE_RESULT (exit {proc.returncode})"),
        }
    finally:
        result_path.unlink(missing_ok=True)

    if proc.returncode != 0 and not result.get("reason"):
        result.setdefault("ok", False)
        result.setdefault("span", None)
        result["reason"] = (
            result.get("reason") or f"playwright exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return result
