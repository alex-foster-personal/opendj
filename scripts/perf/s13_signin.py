"""Headed Google sign-in helper that writes a Playwright storageState for S13 capture.

Operator convention for ``--out`` (not a default): ``~/.local/state/af-perf-kpi/google-storage-state.json``.
Follow-on capture: ``just perf-capture --engine <url> --scenario S13 --google-storage-state <path>``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_REPO = Path(__file__).resolve().parents[2]
_FRONTEND_ROOT = _REPO / "apps" / "webui" / "frontend"
_BROWSER_SCRIPT = _REPO / "scripts" / "perf" / "s13_signin_browser.mjs"

_DOCUMENTED_OUT = "~/.local/state/af-perf-kpi/google-storage-state.json"
_OAUTH_ENV_NAMES = (
    "OPENDJ_GOOGLE_OAUTH_CLIENT_ID",
    "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET",
)


def _http_json(method: str, url: str) -> tuple[int, Any]:
    request = Request(url, headers={"Accept": "application/json"}, method=method)
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


def preflight(frontend: str) -> str | None:
    """Return an error message, or None when the engine is reachable and OAuth is configured."""
    origin = frontend.rstrip("/")
    try:
        status, payload = _http_json("GET", f"{origin}/api/v1/health")
    except ConnectionError as exc:
        return f"engine unreachable: {exc}"
    if status != 200:
        return f"engine health returned HTTP {status}"
    configured = (
        isinstance(payload, dict)
        and payload.get("google_oauth_configured") is True
    )
    if not configured:
        return (
            "Google OAuth is not configured; set "
            f"{_OAUTH_ENV_NAMES[0]} and {_OAUTH_ENV_NAMES[1]}"
        )
    return None


def launch_browser(*, frontend: str, out: Path, timeout_s: int) -> int:
    """Run the Node Playwright helper. Returns the subprocess exit code."""
    proc = subprocess.run(
        [
            "pnpm",
            "exec",
            "node",
            str(_BROWSER_SCRIPT),
            "--frontend",
            frontend,
            "--out",
            str(out),
            "--timeout-s",
            str(timeout_s),
        ],
        cwd=_FRONTEND_ROOT,
        env=os.environ.copy(),
    )
    return proc.returncode


def finalize_storage_state(out: Path) -> int:
    """Set mode 600 and print path plus cookie/origin counts (never values)."""
    os.chmod(out, 0o600)
    payload = json.loads(out.read_text(encoding="utf-8"))
    cookies = payload.get("cookies")
    origins = payload.get("origins")
    cookie_count = len(cookies) if isinstance(cookies, list) else 0
    origin_count = len(origins) if isinstance(origins, list) else 0
    print(f"saved {out} ({cookie_count} cookies, {origin_count} origins)")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Open headed Chromium at the openDJ login page and save a Playwright "
            "storageState for S13 KPI capture."
        ),
    )
    parser.add_argument(
        "--frontend",
        required=True,
        help=(
            "SPA or engine origin (for example http://127.0.0.1:8686). "
            "Probes {origin}/api/v1/health before opening a browser."
        ),
    )
    parser.add_argument(
        "--out",
        required=True,
        help=(
            "Destination path for the Playwright storageState JSON. "
            f"Operator convention: {_DOCUMENTED_OUT}"
        ),
    )
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=600,
        help="Bounded wait for an authenticated session after the window opens (default: 600).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    frontend = args.frontend.rstrip("/")
    out = Path(args.out).expanduser()

    reason = preflight(frontend)
    if reason is not None:
        print(reason, file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    staging = out.parent / f".{out.name}.staging-{os.getpid()}"
    print("Sign in with Google in the opened window.")
    browser_code = launch_browser(frontend=frontend, out=staging, timeout_s=args.timeout_s)
    if browser_code != 0:
        staging.unlink(missing_ok=True)
        return 1
    if not staging.is_file():
        print("browser helper exited without writing storageState", file=sys.stderr)
        staging.unlink(missing_ok=True)
        return 1
    staging.replace(out)
    return finalize_storage_state(out)


if __name__ == "__main__":
    raise SystemExit(main())
