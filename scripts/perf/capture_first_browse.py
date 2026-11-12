"""Drive S9/S11 first-browse capture in Playwright and append KPI ledger rows.

    python -m scripts.perf.capture_first_browse [--dry-run]

Needs a running engine and frontend (the shared runner never starts servers).
The shell launch before navigationStart is not included in either KPI.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from scripts.perf.capture_first_browse_ledger import first_browse_to_ledger_rows
from scripts.perf.capture_kpi_ledger import (
    DEFAULT_LEDGER,
    format_appended,
    git_sha,
    session_meta,
)
from scripts.perf.capture_ledger import append_ledger_rows
from scripts.perf.playwright_capture import run_playwright_capture  # re-exported for tests

_REPO = Path(__file__).resolve().parents[2]
_FRONTEND_ROOT = _REPO / "apps" / "webui" / "frontend"
_PLAYWRIGHT_CONFIG = "tests/e2e/playwright.kpi-first-browse-capture.config.ts"
_PROBE_TIMEOUT_S = 10.0


def _probe_engine(engine: str, timeout_s: float = _PROBE_TIMEOUT_S) -> str | None:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"{engine.rstrip('/')}/api/v1/health",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            if response.status != 200:
                return f"engine health returned HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return f"engine health returned HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return f"engine unreachable: {exc.reason}"
    except OSError as exc:
        return f"engine unreachable: {type(exc).__name__}: {exc}"
    return None


def _run_playwright_capture(
    *,
    engine: str,
    frontend: str,
    timeout_s: int,
) -> dict[str, Any]:
    return run_playwright_capture(
        engine=engine,
        frontend=frontend,
        frontend_root=_FRONTEND_ROOT,
        playwright_config=_PLAYWRIGHT_CONFIG,
        timeout_s=timeout_s,
        google_storage_state=None,
    )


def _both_measured(rows: list[dict[str, Any]]) -> bool:
    return all(row.get("measured") is True and row.get("value") is not None for row in rows)


def capture_first_browse(
    *,
    engine: str,
    frontend: str,
    ledger_path: Path,
    timeout_s: int = 90,
    probe_timeout_s: float = _PROBE_TIMEOUT_S,
    dry_run: bool = False,
) -> int:
    meta = session_meta(sha=git_sha())

    probe_reason = _probe_engine(engine, timeout_s=probe_timeout_s)
    if probe_reason is not None:
        rows = first_browse_to_ledger_rows(None, meta=meta, reason=probe_reason)
        if not dry_run:
            append_ledger_rows(ledger_path, rows)
        for row in rows:
            print(format_appended(row), file=sys.stderr)
        if dry_run:
            print(json.dumps(rows, indent=2))
        return 1

    result = _run_playwright_capture(engine=engine, frontend=frontend, timeout_s=timeout_s)
    first_browse = result.get("first_browse") if isinstance(result.get("first_browse"), dict) else None
    reason = result.get("reason") if isinstance(result.get("reason"), str) else None
    ok = result.get("ok") is True

    rows = first_browse_to_ledger_rows(
        first_browse,
        meta=meta,
        reason=reason if not ok else None,
    )
    for row in rows:
        print(format_appended(row), file=sys.stderr)
    if dry_run:
        print(json.dumps(rows, indent=2))
        return 0 if _both_measured(rows) else 1
    append_ledger_rows(ledger_path, rows)
    return 0 if _both_measured(rows) else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture S9/S11 first-browse KPIs via Playwright")
    parser.add_argument("--engine", default="http://127.0.0.1:8686", help="engine base URL")
    parser.add_argument("--frontend", default="http://127.0.0.1:5273", help="frontend base URL")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--timeout-s", type=int, default=90, dest="timeout_s")
    parser.add_argument("--dry-run", action="store_true", help="print rows, append nothing")
    args = parser.parse_args(list(argv) if argv is not None else None)

    return capture_first_browse(
        engine=args.engine,
        frontend=args.frontend,
        ledger_path=args.ledger,
        timeout_s=args.timeout_s,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
