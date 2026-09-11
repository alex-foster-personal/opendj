"""Capture performance KPIs from a running engine (S5, S12, S13).

Usage:
  python -m scripts.perf.capture_kpis --engine http://127.0.0.1:8686 --scenario S13
  just perf-capture --engine http://127.0.0.1:8686 --scenario S13
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from scripts.perf.capture_s13 import capture_s13

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_LEDGER = _REPO / "docs" / "perf" / "kpi-ledger.json"
_IMPLEMENTED = {"S13"}
_KNOWN_UNIMPLEMENTED = {"S5", "S12"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture performance KPIs from a running engine. "
            "Implemented scenarios: S13 (login submit-to-library-usable)."
        ),
    )
    parser.add_argument(
        "--engine",
        required=True,
        help="running engine origin, e.g. http://127.0.0.1:8686",
    )
    parser.add_argument(
        "--scenario",
        required=True,
        help="comma-separated scenario ids (S13 implemented; S5/S12 pending #1884)",
    )
    parser.add_argument(
        "--frontend",
        default=None,
        help="SPA origin when Vite and the API are on different ports (defaults to --engine)",
    )
    parser.add_argument(
        "--ledger",
        default=str(_DEFAULT_LEDGER),
        help="append target for KPI ledger rows",
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
        help="wait for the perf-span POST after bauble click",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the rows without writing the ledger",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    frontend = (args.frontend or args.engine).rstrip("/")
    engine = args.engine.rstrip("/")
    ledger_path = Path(args.ledger)
    scenarios = [part.strip() for part in args.scenario.split(",") if part.strip()]
    if not scenarios:
        print("capture_kpis: --scenario must name at least one id", file=sys.stderr)
        return 2

    exit_code = 0
    for scenario in scenarios:
        if scenario in _KNOWN_UNIMPLEMENTED:
            print(
                f"capture_kpis: scenario {scenario} is not implemented in this tree "
                "(see issue #1884)",
                file=sys.stderr,
            )
            exit_code = 1
            continue
        if scenario not in _IMPLEMENTED:
            print(f"capture_kpis: unknown scenario {scenario}", file=sys.stderr)
            exit_code = 1
            continue
        if scenario == "S13":
            code = capture_s13(
                engine=engine,
                frontend=frontend,
                ledger_path=ledger_path,
                google_storage_state=args.google_storage_state,
                timeout_s=args.timeout_s,
                dry_run=args.dry_run,
            )
            exit_code = max(exit_code, code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
