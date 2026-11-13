"""PERF-LEAK-11717: reduce a headless leak-cycle probe result (issue #10278).

Input: the JSON written by apps/webui/frontend/tests/e2e/leak-cycle-probe.spec.ts, a
``cycles`` list with ``cycle``, ``js_heap_used_mb``, ``live_handles`` (kind -> count, or
null until window.__mdtLiveHandles() exists) and ``canvas_count`` per cycle.

Metrics, over cycles 3 onward (the first two warm):
- ``js_heap_slope_mb_per_cycle``: least-squares slope of the heap against the cycle number.
- ``handles_delta``: last minus third, per handle kind.
- ``canvas_delta``: last minus third; reported, never gated (canvases belong to rows).

Exit codes: 0 within limits, 1 slope over ``--max-slope-mb`` or a handle kind grown by more
than ``--max-handle-growth``, 2 insufficient cycles (under 5) or a malformed file.

Usage:
    uv run python -m scripts.perf.leak_cycle_report --result <file> [--json]
        [--max-slope-mb N] [--max-handle-growth N]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WARM_CYCLES = 2
MIN_CYCLES = 5
DEFAULT_MAX_SLOPE_MB = 2.0
DEFAULT_MAX_HANDLE_GROWTH = 0
INSUFFICIENT = "insufficient cycles"
UNAVAILABLE = "live handles unavailable"


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def read_result(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cycles = payload.get("cycles") if isinstance(payload, dict) else None
    if not isinstance(cycles, list) or not all(isinstance(row, dict) for row in cycles):
        raise ValueError("result file has no cycles list of objects")
    for index, row in enumerate(cycles):
        if not _is_number(row.get("cycle")):
            raise ValueError(f"cycle row {index} has no numeric cycle")
        if not _is_number(row.get("js_heap_used_mb")):
            raise ValueError(f"cycle {row['cycle']} has no numeric js_heap_used_mb")
    return sorted(cycles, key=lambda row: row["cycle"])


def least_squares_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denom


def handle_totals(cycle: dict[str, Any]) -> dict[str, float] | None:
    handles = cycle.get("live_handles")
    if not isinstance(handles, dict):
        return None
    return {kind: value for kind, value in handles.items() if _is_number(value)}


@dataclass(frozen=True)
class LeakReport:
    cycles: int
    insufficient: bool
    fit_first_cycle: int | None = None
    fit_last_cycle: int | None = None
    js_heap_slope_mb_per_cycle: float | None = None
    heap_first_mb: float | None = None
    heap_last_mb: float | None = None
    handles_delta: dict[str, float] | None = None
    canvas_delta: int | None = None

    @property
    def handles_available(self) -> bool:
        return self.handles_delta is not None


def reduce_cycles(cycles: Sequence[dict[str, Any]]) -> LeakReport:
    if len(cycles) < MIN_CYCLES:
        return LeakReport(cycles=len(cycles), insufficient=True)
    fit = list(cycles[WARM_CYCLES:])
    first, last = fit[0], fit[-1]
    slope = least_squares_slope([float(r["cycle"]) for r in fit], [float(r["js_heap_used_mb"]) for r in fit])
    first_h, last_h = handle_totals(first), handle_totals(last)
    handles_delta = None
    if first_h is not None and last_h is not None:
        handles_delta = {k: last_h.get(k, 0) - first_h.get(k, 0) for k in sorted(set(first_h) | set(last_h))}
    canvas_delta = None
    if _is_number(first.get("canvas_count")) and _is_number(last.get("canvas_count")):
        canvas_delta = int(last["canvas_count"] - first["canvas_count"])
    return LeakReport(
        cycles=len(cycles),
        insufficient=False,
        fit_first_cycle=int(first["cycle"]),
        fit_last_cycle=int(last["cycle"]),
        js_heap_slope_mb_per_cycle=slope,
        heap_first_mb=float(first["js_heap_used_mb"]),
        heap_last_mb=float(last["js_heap_used_mb"]),
        handles_delta=handles_delta,
        canvas_delta=canvas_delta,
    )


def _signed(value: float) -> str:
    return f"{value:+g}"


def verdict(report: LeakReport, max_slope_mb: float, max_handle_growth: int) -> tuple[int, list[str]]:
    if report.insufficient:
        return 2, []
    offenders: list[str] = []
    slope = report.js_heap_slope_mb_per_cycle
    if slope is not None and slope > max_slope_mb:
        offenders.append(f"js_heap_slope_mb_per_cycle {slope:.1f} > {max_slope_mb:g}")
    for kind, delta in (report.handles_delta or {}).items():
        if delta > max_handle_growth:
            offenders.append(f"{kind} {_signed(delta)}")
    return (1 if offenders else 0), offenders


def to_json(report: LeakReport, offenders: list[str], code: int) -> dict[str, Any]:
    slope = report.js_heap_slope_mb_per_cycle
    return {
        "cycles": report.cycles,
        "fit_cycles": None if report.insufficient else [report.fit_first_cycle, report.fit_last_cycle],
        "js_heap_slope_mb_per_cycle": None if slope is None else round(slope, 4),
        "heap_first_mb": report.heap_first_mb,
        "heap_last_mb": report.heap_last_mb,
        "handles_available": report.handles_available,
        "handles_delta": report.handles_delta,
        "canvas_delta": report.canvas_delta,
        "status": INSUFFICIENT if code == 2 else ("over" if code == 1 else "ok"),
        "offenders": offenders,
    }


def to_text(report: LeakReport) -> str:
    if report.insufficient:
        return f"{INSUFFICIENT} ({report.cycles} < {MIN_CYCLES})"
    lines = [
        f"js_heap_slope_mb_per_cycle {report.js_heap_slope_mb_per_cycle:.1f} "
        f"(cycles {report.fit_first_cycle}..{report.fit_last_cycle})"
    ]
    if report.handles_delta is None:
        lines.append(f"handles_delta {UNAVAILABLE}")
    else:
        lines.append("handles_delta " + ", ".join(f"{k} {_signed(v)}" for k, v in report.handles_delta.items()))
    canvas = "n/a" if report.canvas_delta is None else _signed(report.canvas_delta)
    lines.append(f"canvas_delta {canvas} (reported, not gated)")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-slope-mb", type=float, default=DEFAULT_MAX_SLOPE_MB)
    parser.add_argument("--max-handle-growth", type=int, default=DEFAULT_MAX_HANDLE_GROWTH)
    args = parser.parse_args(argv)
    try:
        cycles = read_result(args.result)
    except (OSError, ValueError) as exc:
        parser.error(f"cannot read {args.result}: {exc}")
    report = reduce_cycles(cycles)
    code, offenders = verdict(report, args.max_slope_mb, args.max_handle_growth)
    if args.json:
        print(json.dumps(to_json(report, offenders, code), indent=2))
    else:
        print(to_text(report))
    if not report.insufficient and not report.handles_available:
        print(UNAVAILABLE, file=sys.stderr)
    if code == 1:
        print("over limit: " + "; ".join(offenders), file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
