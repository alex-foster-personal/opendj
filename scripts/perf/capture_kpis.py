"""Capture S2, S5, S12, and S13 KPIs against a RUNNING engine.

Drives real HTTP/CLI endpoints only. ``--engine`` is required (no hidden
port fallback). An unreachable engine, a missing track, or a failed probe
writes an error (S5/S12) or withheld (S2/S13) row, never a number.

Usage::

    python -m scripts.perf.capture_kpis --engine <base-url> --scenario S5,S12
    python -m scripts.perf.capture_kpis --engine <base-url> --scenario S2
    python -m scripts.perf.capture_kpis --engine <base-url> --scenario S13
    just perf-capture --engine <base-url> --scenario S2
    just perf-capture --engine <base-url> --scenario S13
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import URLError

from scripts.perf import capture_s5, capture_s12
from scripts.perf.capture_boot_library import capture_boot_library
from scripts.perf.capture_kpi_ledger import (
    DEFAULT_LEDGER,
    HEALTH_TIMEOUT_S,
    CaptureMeta,
    append_entries,
    fetch_url,
    format_appended,
    git_sha,
    has_error_row,
    required_error_rows,
    required_numeric_present,
    session_meta,
)
from scripts.perf.capture_s2 import capture_s2
from scripts.perf.capture_s13 import capture_s13

KNOWN_SCENARIOS = ("S2", "S5", "S12", "S13", "boot-library")
HTTP_SCENARIOS = frozenset({"S5", "S12"})


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--engine",
        required=True,
        help="running engine base URL (no hidden 8686 fallback)",
    )
    parser.add_argument(
        "--scenario",
        default="S5,S12",
        help="comma-separated scenario ids (default S5,S12; S13 is login)",
    )
    parser.add_argument(
        "--hub",
        default=None,
        help="CloudSync hub URL; required when S12 is selected",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="engine data dir holding state/state.db; default from /health",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER,
        help="kpi-ledger.json path (tests pass a temp file)",
    )
    parser.add_argument("--track-small", default=None, help="override CFG small id")
    parser.add_argument("--track-large", default=None, help="override CFG large id")
    parser.add_argument("--track-stemmed", default=None, help="override CFG stemmed id")
    parser.add_argument(
        "--frontend",
        default=None,
        help="SPA origin when Vite and the API are on different ports (defaults to --engine)",
    )
    parser.add_argument(
        "--google-storage-state",
        default=os.environ.get("OPENDJ_KPI_GOOGLE_STORAGE_STATE"),
        help=(
            "Playwright storageState JSON with a pre-consented Google session "
            "(default: OPENDJ_KPI_GOOGLE_STORAGE_STATE)"
        ),
    )
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=90,
        help="Playwright budget for S2 capture or S13 perf-span wait",
    )
    parser.add_argument(
        "--presses",
        type=int,
        default=32,
        help="number of complete-floor ordinary press rows required for S2",
    )
    parser.add_argument(
        "--browser",
        default="webkit",
        choices=("webkit", "chromium"),
        help="browser project for S2 capture (default webkit)",
    )
    parser.add_argument(
        "--track",
        default=None,
        help="override stable_id for S2 deck load (default first present track)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the rows without writing the ledger",
    )
    return parser.parse_args(argv)


def parse_scenarios(raw: str) -> list[str] | None:
    parts = [item.strip() for item in raw.split(",") if item.strip()]
    if not parts:
        print("[ERROR] --scenario is empty", file=sys.stderr)
        return None
    unknown = [item for item in parts if item not in KNOWN_SCENARIOS]
    if unknown:
        print(
            f"[ERROR] unknown scenario {', '.join(unknown)}. Known: {', '.join(KNOWN_SCENARIOS)}",
            file=sys.stderr,
        )
        return None
    # Preserve order, drop duplicates.
    seen: list[str] = []
    for item in parts:
        if item not in seen:
            seen.append(item)
    return seen


def strip_engine(url: str) -> str:
    return url.rstrip("/")


def resolve_data_dir(cli: Path | None, health: dict[str, Any]) -> Path | None:
    if cli is not None:
        return Path(cli)
    raw = (health.get("state_db") or {}).get("path")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        return None
    if path.name == "state.db" and path.parent.name == "state":
        return path.parent.parent
    return None


def health_track_count(health: dict[str, Any]) -> int | None:
    raw = (health.get("state_db") or {}).get("tracks")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def probe_health(engine: str) -> tuple[dict[str, Any] | None, str | None]:
    url = f"{engine}/api/v1/health"
    try:
        status, _headers, body = fetch_url(url, HEALTH_TIMEOUT_S)
    except URLError as exc:
        return None, f"engine unreachable: {exc}"
    except OSError as exc:
        return None, f"engine unreachable: {exc}"
    if status != 200:
        return None, f"engine unreachable: GET /api/v1/health returned HTTP {status}"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"engine unreachable: /health was not JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "engine unreachable: /health JSON was not an object"
    if payload.get("status") != "ok":
        return None, f"engine unreachable: /health status={payload.get('status')!r}"
    state_db = payload.get("state_db")
    if not isinstance(state_db, dict):
        return None, "engine unreachable: /health missing state_db"
    return payload, None


def _finish(ledger: Path, rows: list[dict[str, Any]], dry_run: bool) -> None:
    if rows and not dry_run:
        append_entries(ledger, rows)
    for row in rows:
        print(format_appended(row))


def build_s5_rows(
    *,
    engine: str,
    data_dir: Path | None,
    small: str | None,
    large: str | None,
    stemmed: str | None,
    sha: str,
) -> list[dict[str, Any]]:
    meta = session_meta(sha=sha)
    tracks = capture_s5.resolve_tracks(small=small, large=large, stemmed=stemmed)
    return capture_s5.capture(
        engine=strip_engine(engine),
        meta=meta,
        tracks=tracks,
        data_dir=data_dir,
    )


def capture_s5_against_engine(
    *,
    engine: str,
    ledger: Path,
    data_dir: Path | None,
    small: str | None,
    large: str | None,
    stemmed: str | None,
    sha: str,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    rows = build_s5_rows(
        engine=engine,
        data_dir=data_dir,
        small=small,
        large=large,
        stemmed=stemmed,
        sha=sha,
    )
    _finish(ledger, rows, dry_run)
    return rows


def _run_http_scenarios(
    scenarios: list[str],
    args: argparse.Namespace,
    meta: CaptureMeta,
    health: dict[str, Any],
    engine: str,
) -> list[dict[str, Any]]:
    data_dir = resolve_data_dir(args.data_dir, health)
    rows: list[dict[str, Any]] = []
    if "S5" in scenarios:
        rows.extend(
            build_s5_rows(
                engine=engine,
                data_dir=data_dir,
                small=args.track_small,
                large=args.track_large,
                stemmed=args.track_stemmed,
                sha=meta.sha,
            )
        )
    if "S12" in scenarios:
        rows.extend(
            capture_s12.capture(
                hub_url=args.hub,
                meta=meta,
                data_dir=data_dir,
                track_count=health_track_count(health),
            )
        )
    return rows


def _run_s5_s12(args: argparse.Namespace, scenarios: list[str], engine: str) -> int:
    sha = git_sha()
    meta = session_meta(sha=sha)
    health, health_err = probe_health(engine)
    if health_err is not None:
        _finish(args.ledger, required_error_rows(scenarios, meta, health_err), args.dry_run)
        return 1
    if not sha:
        rows = required_error_rows(scenarios, meta, "git sha cannot be read")
        _finish(args.ledger, rows, args.dry_run)
        return 1
    assert health is not None
    rows = _run_http_scenarios(scenarios, args, meta, health, engine)
    _finish(args.ledger, rows, args.dry_run)
    if has_error_row(rows) or not required_numeric_present(scenarios, rows):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    scenarios = parse_scenarios(args.scenario)
    if scenarios is None:
        return 2
    engine = strip_engine(args.engine)
    exit_code = 0
    http_scenarios = [item for item in scenarios if item in HTTP_SCENARIOS]
    if http_scenarios:
        exit_code = max(exit_code, _run_s5_s12(args, http_scenarios, engine))
    frontend = (args.frontend or args.engine).rstrip("/")
    if "S2" in scenarios:
        code = capture_s2(
            engine=engine,
            frontend=frontend,
            ledger_path=Path(args.ledger),
            presses=args.presses,
            browser=args.browser,
            timeout_s=args.timeout_s,
            track=args.track,
            dry_run=args.dry_run,
        )
        exit_code = max(exit_code, code)
    if "S13" in scenarios:
        code = capture_s13(
            engine=engine,
            frontend=frontend,
            ledger_path=Path(args.ledger),
            google_storage_state=args.google_storage_state,
            timeout_s=args.timeout_s,
            dry_run=args.dry_run,
        )
        exit_code = max(exit_code, code)
    if "boot-library" in scenarios:
        code = capture_boot_library(
            engine=engine,
            frontend=frontend,
            ledger_path=Path(args.ledger),
            timeout_s=args.timeout_s,
            dry_run=args.dry_run,
        )
        exit_code = max(exit_code, code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
