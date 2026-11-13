"""Print the ahead-analysis drain's tracks per minute per phase (PERF-DRAIN-9996).

Reads ``throughput`` from ``GET /api/v1/ahead-analysis/coverage`` of a running
engine (``--engine http://127.0.0.1:<port>``) or from a saved copy of that JSON
(``--json path``) and prints one row per phase. Measure-only: it never changes
the drain.

    uv run python -m scripts.perf.analysis_throughput --engine http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

COVERAGE_PATH = "/api/v1/ahead-analysis/coverage"


def read_status(engine: str | None, json_path: Path | None) -> dict[str, Any]:
    """The coverage JSON, from the engine or a saved file."""
    if json_path is not None:
        return json.loads(json_path.read_text(encoding="utf-8"))
    if engine is None:
        raise ValueError("give --engine URL or --json PATH")
    with urllib.request.urlopen(engine.rstrip("/") + COVERAGE_PATH, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def format_table(status: dict[str, Any]) -> str:
    """One aligned row per phase: tracks per minute (10 min), p50 wall ms, total."""
    throughput: dict[str, dict[str, Any]] = status.get("throughput") or {}
    lines = [f"{'phase':<12} {'tracks/min (10m)':>16} {'p50 wall ms':>12} {'total':>8}"]
    for phase in sorted(throughput):
        row = throughput[phase]
        p50 = row.get("p50_wall_ms")
        p50_text = "-" if p50 is None else f"{float(p50):.0f}"
        lines.append(
            f"{phase:<12} {float(row.get('tracks_per_min_10m') or 0.0):>16.2f} {p50_text:>12} "
            f"{int(row.get('completed_total') or 0):>8}"
        )
    if not throughput:
        lines.append("(no completions yet)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Read the status and print the table."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--engine", help="engine base URL, e.g. http://127.0.0.1:8765")
    source.add_argument("--json", type=Path, help="a saved /ahead-analysis/coverage response")
    args = parser.parse_args(argv)
    print(format_table(read_status(args.engine, args.json)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
