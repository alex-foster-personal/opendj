"""Drive a real Google login in Playwright and capture S13 login KPI spans.

Requires a running engine with Google OAuth configured
(OPENDJ_GOOGLE_OAUTH_CLIENT_ID / OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET).
For unattended capture, set OPENDJ_KPI_GOOGLE_STORAGE_STATE to a Playwright
storageState JSON path with a pre-consented Google session. Create that file
with ``python -m scripts.perf.s13_signin`` (``just perf-s13-signin``).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scripts.perf.capture_ledger import append_ledger_rows, span_to_ledger_rows

_REPO = Path(__file__).resolve().parents[2]
_FRONTEND_ROOT = _REPO / "apps" / "webui" / "frontend"
_PLAYWRIGHT_CONFIG = "tests/e2e/playwright.kpi-capture.config.ts"


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


def _http_json(method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
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


def _probe_engine(engine: str, frontend: str) -> str | None:
    try:
        status, _payload = _http_json("GET", f"{engine.rstrip('/')}/api/v1/health")
    except ConnectionError as exc:
        return f"engine unreachable: {exc}"
    if status != 200:
        return f"engine health returned HTTP {status}"
    try:
        status, payload = _http_json(
            "POST",
            f"{engine.rstrip('/')}/api/v1/auth/login",
            {"origin": frontend.rstrip("/")},
        )
    except ConnectionError as exc:
        return f"engine unreachable: {exc}"
    if status == 503 and isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict) and detail.get("code") == "AUTH_NOT_CONFIGURED":
            return "AUTH_NOT_CONFIGURED"
    if status != 200:
        return f"auth login probe returned HTTP {status}"
    return None


def _run_playwright_capture(
    *,
    engine: str,
    frontend: str,
    google_storage_state: str | None,
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
    if google_storage_state:
        env["KPI_CAPTURE_GOOGLE_STORAGE_STATE"] = google_storage_state
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


def capture_s13(
    *,
    engine: str,
    frontend: str,
    ledger_path: Path,
    google_storage_state: str | None = None,
    timeout_s: int = 90,
    dry_run: bool = False,
) -> int:
    """Capture S13 login KPI and append ledger rows. Returns process exit code."""
    sha = _git_sha()
    machine = _machine_name()
    capture_date = _utc_today()

    probe_reason = _probe_engine(engine, frontend)
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
        return 1

    result = _run_playwright_capture(
        engine=engine,
        frontend=frontend,
        google_storage_state=google_storage_state,
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
            reason=reason,
        )
        if not dry_run:
            append_ledger_rows(ledger_path, rows)
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
