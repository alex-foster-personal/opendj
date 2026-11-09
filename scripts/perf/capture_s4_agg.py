"""Pure aggregation for S4 waveform smoothness KPIs from PERFPROBE-01 frame probe windows.

Mini-PRD
--------
S4 in ``docs/perf/kpi-map.json`` requires ``waveform_frame_delta_ms_p95``,
``waveform_dropped_frame_pct_10s`` and ``waveform_frame_delta_ms_max``. The opt-in frame probe
(``?perfprobe=1``) already summarises each window into the client performance sample
(``GET /api/v1/performance/telemetry/client-samples``); this module reduces the windows taken
while a deck plays into those three ledger rows.

Requirements:
- [ ] [if] a window's frames over ``window_ms`` say 120 Hz [then] a 12 ms frame counts as dropped,
  while the same 12 ms at 60 Hz does not: the refresh interval is measured per window
  (``window_ms / frames``), never a fixed 16.7 ms.
- [ ] [if] several playing windows are read [then] ``waveform_frame_delta_ms_max`` is the single
  worst ``max_ms`` among them, not a percentile.
- [ ] [if] no deck is playing in a sample [then] its window is excluded; zero playing windows
  yields withheld rows that say why.

Dropped frames come from the window summary, which carries percentiles rather than every delta,
so the count is a LOWER BOUND: a frame is dropped when its delta exceeds ``DROP_FACTOR`` times the
window's refresh interval, and the summary proves at least 50% of sampled frames dropped when
p50 is over that line, 5% when p95 is, 1% when p99 is, ``long_frames`` when the line is under
the probe's 50 ms long-frame bar, and one frame when only ``max_ms`` is. ``DROP_FACTOR`` 1.25
sits between one vsync (1.0x, plus jitter) and two (2.0x). The per-window p95 and dropped
percentage are reported for the WORST playing window, so one bad 10 s window cannot be averaged
away.

Usage::

    uv run python -m scripts.perf.capture_s4_agg --samples client-samples.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.perf.capture_kpi_ledger import CaptureMeta, build_row, withheld_row

P95_KPI = "waveform_frame_delta_ms_p95"
DROPPED_KPI = "waveform_dropped_frame_pct_10s"
MAX_KPI = "waveform_frame_delta_ms_max"
P95_UNIT = "ms p95 frame delta"
DROPPED_UNIT = "% dropped frames per 10s window"
MAX_UNIT = "ms max frame delta per 10s window"
S4_METHOD = (
    "PERFPROBE-01 in-page rAF frame probe windows from client performance samples taken while a deck "
    "plays; refresh interval measured per window as window_ms / frames; worst playing window"
)
DROP_FACTOR = 1.25
LONG_FRAME_MS = 50.0  # FRAME_PROBE LONG_FRAME_MS in apps/webui/frontend/src/lib/rb/frame-probe.ts


@dataclass(frozen=True)
class WindowScore:
    refresh_interval_ms: float
    p95_ms: float
    max_ms: float
    dropped_frames: int
    dropped_pct: float


# ----- window scoring


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def deck_is_playing(sample: dict[str, Any]) -> bool:
    decks = sample.get("decks")
    return isinstance(decks, list) and any(isinstance(deck, dict) and deck.get("playing") is True for deck in decks)


def dropped_frames_lower_bound(window: dict[str, Any], threshold_ms: float) -> int:
    """The fewest frames over ``threshold_ms`` the window summary proves (see module docstring)."""
    sampled = int(window.get("frames_sampled") or window.get("frames") or 0)
    bounds = [0]
    for key, share in (("p50_ms", 0.50), ("p95_ms", 0.05), ("p99_ms", 0.01)):
        value = _number(window.get(key))
        if value is not None and value > threshold_ms:
            bounds.append(math.ceil(share * sampled))
    max_ms = _number(window.get("max_ms"))
    if max_ms is not None and max_ms > threshold_ms:
        bounds.append(1)
    if threshold_ms <= LONG_FRAME_MS:
        bounds.append(int(window.get("long_frames") or 0))
    return max(bounds)


def score_window(window: dict[str, Any]) -> WindowScore | None:
    """Score one frame probe window, or None when it carries no usable frames."""
    frames = int(window.get("frames") or 0)
    window_ms = _number(window.get("window_ms"))
    p95_ms = _number(window.get("p95_ms"))
    max_ms = _number(window.get("max_ms"))
    if frames <= 0 or window_ms is None or window_ms <= 0 or p95_ms is None or max_ms is None:
        return None
    refresh_interval_ms = window_ms / frames
    dropped = dropped_frames_lower_bound(window, DROP_FACTOR * refresh_interval_ms)
    return WindowScore(
        refresh_interval_ms=round(refresh_interval_ms, 3),
        p95_ms=p95_ms,
        max_ms=max_ms,
        dropped_frames=dropped,
        dropped_pct=round(100.0 * dropped / frames, 3),
    )


def playing_windows(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        sample["frame_probe"]
        for sample in samples
        if deck_is_playing(sample) and isinstance(sample.get("frame_probe"), dict)
    ]


# ----- ledger rows


def withheld_rows(reason: str, meta: CaptureMeta) -> list[dict[str, Any]]:
    return [
        withheld_row(kpi=kpi, unit=unit, method=S4_METHOD, meta=meta, reason=reason)
        for kpi, unit in ((P95_KPI, P95_UNIT), (DROPPED_KPI, DROPPED_UNIT), (MAX_KPI, MAX_UNIT))
    ]


def samples_to_ledger_rows(samples: list[dict[str, Any]], meta: CaptureMeta) -> list[dict[str, Any]]:
    """Three S4 ledger rows from client performance samples, withheld when nothing played."""
    windows = playing_windows(samples)
    if not windows:
        return withheld_rows("no frame probe window was taken while a deck was playing", meta)
    scores = [score for score in (score_window(window) for window in windows) if score is not None]
    if not scores:
        return withheld_rows(f"{len(windows)} playing frame probe windows carried no frames", meta)
    worst_p95 = max(scores, key=lambda score: score.p95_ms)
    worst_dropped = max(scores, key=lambda score: score.dropped_pct)
    worst_max = max(score.max_ms for score in scores)
    note = (
        f"n={len(scores)} playing windows; worst-window p95 at refresh {worst_p95.refresh_interval_ms} ms; "
        f"dropped = delta > {DROP_FACTOR}x measured refresh (lower bound from the window summary)"
    )
    return [
        build_row(kpi=P95_KPI, value=worst_p95.p95_ms, unit=P95_UNIT, method=S4_METHOD, meta=meta, note=note),
        build_row(kpi=DROPPED_KPI, value=worst_dropped.dropped_pct, unit=DROPPED_UNIT, method=S4_METHOD, meta=meta,
                  note=note),
        build_row(kpi=MAX_KPI, value=worst_max, unit=MAX_UNIT, method=S4_METHOD, meta=meta, note=note),
    ]


# ----- CLI


def load_samples(path: Path) -> list[dict[str, Any]]:
    """A JSON list of samples, one sample object, or JSON lines."""
    text = path.read_text()
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
    samples = parsed if isinstance(parsed, list) else [parsed]
    if not all(isinstance(sample, dict) for sample in samples):
        raise ValueError(f"{path} must hold client performance sample objects")
    return samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", type=Path, required=True, help="client performance samples (JSON or JSONL)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the rows; appending to the ledger is the live capture's job")
    parser.add_argument("--machine", default="unknown")
    parser.add_argument("--sha", default="unknown")
    args = parser.parse_args(argv)
    if not args.dry_run:
        parser.error("only --dry-run is supported: this aggregator prints rows and never appends to the ledger")
    now = datetime.now(UTC)
    meta = CaptureMeta(capture_id=f"s4-agg-{now:%Y%m%dT%H%M%SZ}", date=now.date().isoformat(),
                       machine=args.machine, sha=args.sha)
    for row in samples_to_ledger_rows(load_samples(args.samples), meta):
        print(json.dumps(row, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
