"""PERF-LEAK-10038: per-role memory slope (MB per hour) over the probe log, idle windows only.

The performance epic's leak metric is "RSS slope per role": under 10 MB per hour idle is the
target, above 50 MB per hour is the hard veto. The diagnostics probe writes one JSONL row per
sample with a ``timestamp`` and a ``processes`` list whose members carry ``role`` and
``physical_footprint_mb``. This command groups those members by role (summing members that
share a role in one sample), keeps only rows inside the explicit ``--idle-window START,END``
ranges (ISO times, repeatable, inclusive), and prints per role and window the first and last
footprint and the least-squares slope. Windows are fitted separately so a jump during churn
between two windows never moves a slope.

    uv run python -m scripts.perf.role_memory_slope --log probe.jsonl \
        --idle-window 2026-11-08T10:00:00Z,2026-11-08T11:00:00Z [--max-slope-mb-per-hour 50] [--json]

Exit 1 when any role's worst window slope exceeds ``--max-slope-mb-per-hour``. A window with
fewer than ``MIN_POINTS`` samples for a role reports "insufficient samples" instead of a slope.
Pure standard library apart from the shared slope helper.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.diagnostics.probe_log_store import _linear_slope_mb_per_hour

DEFAULT_MAX_SLOPE_MB_PER_HOUR = 50.0
TARGET_SLOPE_MB_PER_HOUR = 10.0
MIN_POINTS = 3
INSUFFICIENT = "insufficient samples"


@dataclass(frozen=True)
class IdleWindow:
    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment <= self.end


@dataclass(frozen=True)
class WindowSlope:
    window: IdleWindow
    points: int
    first_mb: float | None
    last_mb: float | None
    slope_mb_per_hour: float | None


@dataclass(frozen=True)
class RoleSlope:
    role: str
    windows: list[WindowSlope]

    @property
    def points(self) -> int:
        return sum(window.points for window in self.windows)

    @property
    def slope_mb_per_hour(self) -> float | None:
        """The worst (largest) window slope; None when no window had enough samples."""
        slopes = [window.slope_mb_per_hour for window in self.windows if window.slope_mb_per_hour is not None]
        return max(slopes) if slopes else None

    @property
    def first_mb(self) -> float | None:
        return next((window.first_mb for window in self.windows if window.first_mb is not None), None)

    @property
    def last_mb(self) -> float | None:
        return next((window.last_mb for window in reversed(self.windows) if window.last_mb is not None), None)


# -----------------------------------------------------------------------------
# helpers


def _parse_time(text: str) -> datetime:
    moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"idle window time {text!r} needs a UTC offset or Z")
    return moment


def parse_idle_window(text: str) -> IdleWindow:
    """``START,END`` ISO times -> an inclusive window; END before START is refused."""
    parts = text.split(",")
    if len(parts) != 2:
        raise ValueError(f"--idle-window takes START,END, got {text!r}")
    window = IdleWindow(_parse_time(parts[0]), _parse_time(parts[1]))
    if window.end <= window.start:
        raise ValueError(f"--idle-window end must be after start, got {text!r}")
    return window


def _role_footprints(record: dict[str, Any]) -> dict[str, float]:
    per_role: dict[str, float] = {}
    for process in record.get("processes") or []:
        if not isinstance(process, dict):
            continue
        role, value = process.get("role"), process.get("physical_footprint_mb")
        if isinstance(role, str) and isinstance(value, (int, float)):
            per_role[role] = per_role.get(role, 0.0) + float(value)
    return per_role


def read_probe_rows(path: Path) -> list[dict[str, Any]]:
    """Every JSON object row in a probe log; a malformed line fails naming its number."""
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{number}: not JSON ({error.msg})") from error
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _window_slope(window: IdleWindow, series: list[tuple[datetime, float]]) -> WindowSlope:
    inside = sorted((moment, value) for moment, value in series if window.contains(moment))
    if not inside:
        return WindowSlope(window, 0, None, None, None)
    first, last = inside[0][1], inside[-1][1]
    if len(inside) < MIN_POINTS:
        return WindowSlope(window, len(inside), first, last, None)
    elapsed = [(moment - inside[0][0]).total_seconds() for moment, _ in inside]
    slope = _linear_slope_mb_per_hour(elapsed, [value for _, value in inside])
    return WindowSlope(window, len(inside), first, last, slope)


# -----------------------------------------------------------------------------
# public


def role_slopes(rows: Iterable[dict[str, Any]], windows: Sequence[IdleWindow]) -> list[RoleSlope]:
    """Per role, the footprint slope inside each idle window, roles sorted by name."""
    if not windows:
        raise ValueError("at least one --idle-window is required")
    series: dict[str, list[tuple[datetime, float]]] = {}
    for row in rows:
        stamp = row.get("timestamp")
        if not isinstance(stamp, str):
            continue
        moment = _parse_time(stamp)
        if not any(window.contains(moment) for window in windows):
            continue
        for role, value in _role_footprints(row).items():
            series.setdefault(role, []).append((moment, value))
    return [
        RoleSlope(role, [_window_slope(window, points) for window in windows])
        for role, points in sorted(series.items())
    ]


def over_limit(slopes: Sequence[RoleSlope], max_slope_mb_per_hour: float) -> list[str]:
    """Roles whose worst window slope exceeds the limit."""
    return [s.role for s in slopes if s.slope_mb_per_hour is not None and s.slope_mb_per_hour > max_slope_mb_per_hour]


def to_json(slopes: Sequence[RoleSlope]) -> dict[str, dict[str, Any]]:
    return {
        s.role: {
            "slope_mb_per_hour": s.slope_mb_per_hour,
            "first_mb": s.first_mb,
            "last_mb": s.last_mb,
            "points": s.points,
            "windows": [
                {
                    "start": w.window.start.isoformat(),
                    "end": w.window.end.isoformat(),
                    "points": w.points,
                    "first_mb": w.first_mb,
                    "last_mb": w.last_mb,
                    "slope_mb_per_hour": w.slope_mb_per_hour,
                }
                for w in s.windows
            ],
        }
        for s in slopes
    }


def _mb(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def to_text(slopes: Sequence[RoleSlope]) -> str:
    lines: list[str] = []
    for s in slopes:
        for w in s.windows:
            slope = INSUFFICIENT if w.slope_mb_per_hour is None else f"{w.slope_mb_per_hour:.1f} MB/h"
            span = f"{w.window.start.isoformat()}..{w.window.end.isoformat()}"
            lines.append(
                f"{s.role}  {span}  points {w.points}  first {_mb(w.first_mb)} MB  last {_mb(w.last_mb)} MB  slope {slope}"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="role_memory_slope", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--log", type=Path, required=True, help="probe JSONL file")
    parser.add_argument(
        "--idle-window", action="append", required=True, type=parse_idle_window, help="START,END ISO times (repeatable)"
    )
    parser.add_argument("--max-slope-mb-per-hour", type=float, default=DEFAULT_MAX_SLOPE_MB_PER_HOUR)
    parser.add_argument("--json", action="store_true", help="machine output")
    args = parser.parse_args(argv)
    slopes = role_slopes(read_probe_rows(args.log), args.idle_window)
    print(json.dumps(to_json(slopes), indent=2, sort_keys=True) if args.json else to_text(slopes))
    offenders = over_limit(slopes, args.max_slope_mb_per_hour)
    if offenders:
        print(f"over {args.max_slope_mb_per_hour:g} MB/h: {', '.join(offenders)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
