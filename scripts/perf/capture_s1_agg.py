"""Pure aggregation for S1 audible integrity KPIs from stored client performance samples.

Mini-PRD
--------
S1 in ``docs/perf/kpi-map.json`` requires ``xruns_per_playing_hour`` and
``silent_while_playing_events_per_hour``. Every client performance sample
(``GET /api/v1/performance/telemetry/client-samples``, stored as JSONL) carries per-deck
``playing`` / ``audible`` / ``transport_pending``, ``audio_health_level``, and, when the page was
opened with ``?perfprobe=1``, a PERFPROBE-01 frame probe window with ``window_ms`` and ``xruns``.
This module reduces those samples into the two S1 ledger rows.

Requirements:
- [ ] [if] xruns land in idle windows [then] they are not counted: only windows where at least
  one deck plays contribute xruns and playing time.
- [ ] [if] every playing window carries ``xruns: null`` (probe or sentinel off) [then] the xrun
  row is withheld naming "frame probe off", never a 0.
- [ ] [if] under ``MIN_PLAYING_MINUTES`` playing minutes were seen [then] the row is withheld
  naming the playing minutes seen.

Silent-while-playing events are counted from the same samples as rising edges, per client
session: a deck turning ``playing`` but not ``audible`` and not ``transport_pending`` (silent
output), and ``audio_health_level`` turning ``crit`` while a deck plays (presentation clock
stalled under 30 Hz). Samples land every 10 s, so both counts are LOWER BOUNDS: a glitch that
starts and ends between two samples is not seen.

Usage::

    uv run python -m scripts.perf.capture_s1_agg --samples client-samples.json --dry-run
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
from scripts.perf.capture_s4_agg import deck_is_playing, load_samples

XRUN_KPI = "xruns_per_playing_hour"
SILENT_KPI = "silent_while_playing_events_per_hour"
S1_UNIT = "events per playing hour"
XRUN_METHOD = (
    "PERFPROBE-01 frame probe windows from client performance samples; sum of window xruns over "
    "windows where a deck plays, divided by those windows' window_ms in hours"
)
SILENT_METHOD = (
    "client performance samples; rising edges per session of a playing deck that is not audible "
    "and not transport_pending, plus audio_health_level turning crit while a deck plays, divided "
    "by playing hours (10 s sampling, lower bound)"
)
#: SAMPLE_INTERVAL_MS in apps/webui/frontend/src/lib/rb/client-performance-samples.ts; the
#: window a sample stands for when it carries no frame probe window.
SAMPLE_INTERVAL_MS = 10_000.0
MIN_PLAYING_MINUTES = 10.0
MS_PER_HOUR = 3_600_000.0


@dataclass(frozen=True)
class S1Tally:
    playing_ms: float
    probed_playing_ms: float
    playing_xruns: int
    silent_events: int
    stalled_events: int


def _number(value: object) -> float | None:
    """A finite int or float, else None (bools are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _window_ms(sample: dict[str, Any]) -> float:
    """The span a sample stands for: its frame probe window, else the sampling interval."""
    probe = sample.get("frame_probe")
    if isinstance(probe, dict):
        value = _number(probe.get("window_ms"))
        if value is not None and value > 0:
            return value
    return SAMPLE_INTERVAL_MS


def _window_xruns(sample: dict[str, Any]) -> int | None:
    """The window's xrun count, or None when the probe or the sentinel was off."""
    probe = sample.get("frame_probe")
    if not isinstance(probe, dict):
        return None
    value = probe.get("xruns")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def silent_decks(sample: dict[str, Any]) -> set[object]:
    """Deck ids that play but are neither audible nor waiting on a transport change."""
    decks = sample.get("decks")
    if not isinstance(decks, list):
        return set()
    return {
        deck.get("deck_id")
        for deck in decks
        if isinstance(deck, dict)
        and deck.get("playing") is True
        and deck.get("audible") is False
        and deck.get("transport_pending") is not True
    }


def _by_session(samples: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Samples grouped by client session, each group in client timestamp order."""
    sessions: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        sessions.setdefault(str(sample.get("client_session_id") or ""), []).append(sample)
    return [sorted(group, key=lambda s: str(s.get("client_timestamp") or "")) for group in sessions.values()]


def tally(samples: list[dict[str, Any]]) -> S1Tally:
    """Playing time, playing-window xruns and silent/stalled rising edges over all sessions."""
    playing_ms = probed_ms = 0.0
    xruns = silent = stalled = 0
    for session in _by_session(samples):
        was_silent: set[object] = set()
        was_stalled = False
        for sample in session:
            playing = deck_is_playing(sample)
            now_silent = silent_decks(sample)
            now_stalled = playing and sample.get("audio_health_level") == "crit"
            silent += len(now_silent - was_silent)
            stalled += int(now_stalled and not was_stalled)
            was_silent, was_stalled = now_silent, now_stalled
            if not playing:
                continue
            window = _window_ms(sample)
            playing_ms += window
            count = _window_xruns(sample)
            if count is not None:
                probed_ms += window
                xruns += count
    return S1Tally(playing_ms, probed_ms, xruns, silent, stalled)


def _minutes(ms: float) -> float:
    """Milliseconds as minutes, one decimal."""
    return round(ms / 60_000.0, 1)


def _per_hour(count: int, ms: float) -> float:
    """A count as a rate per hour of ``ms``."""
    return round(count * MS_PER_HOUR / ms, 3)


def _too_short(ms: float) -> str | None:
    """The withhold reason when under the minimum playing minutes, else None."""
    if ms >= MIN_PLAYING_MINUTES * 60_000.0:
        return None
    return f"only {_minutes(ms)} playing minutes seen, {MIN_PLAYING_MINUTES:g} needed"


def xrun_row(counts: S1Tally, meta: CaptureMeta) -> dict[str, Any]:
    """The ``xruns_per_playing_hour`` row, withheld when the probe was off or play was short."""
    if counts.playing_ms <= 0:
        return withheld_row(
            kpi=XRUN_KPI,
            unit=S1_UNIT,
            method=XRUN_METHOD,
            meta=meta,
            reason="no sample was taken while a deck was playing (0.0 playing minutes)",
        )
    if counts.probed_playing_ms <= 0:
        return withheld_row(
            kpi=XRUN_KPI,
            unit=S1_UNIT,
            method=XRUN_METHOD,
            meta=meta,
            reason="frame probe off: every playing window carried xruns null (open with ?perfprobe=1)",
        )
    reason = _too_short(counts.probed_playing_ms)
    if reason is not None:
        return withheld_row(kpi=XRUN_KPI, unit=S1_UNIT, method=XRUN_METHOD, meta=meta, reason=reason)
    note = f"{counts.playing_xruns} xruns over {_minutes(counts.probed_playing_ms)} probed playing minutes"
    return build_row(
        kpi=XRUN_KPI,
        value=_per_hour(counts.playing_xruns, counts.probed_playing_ms),
        unit=S1_UNIT,
        method=XRUN_METHOD,
        meta=meta,
        note=note,
    )


def silent_row(counts: S1Tally, meta: CaptureMeta) -> dict[str, Any]:
    """The ``silent_while_playing_events_per_hour`` row, withheld when play was short."""
    reason = _too_short(counts.playing_ms)
    if reason is not None:
        return withheld_row(kpi=SILENT_KPI, unit=S1_UNIT, method=SILENT_METHOD, meta=meta, reason=reason)
    events = counts.silent_events + counts.stalled_events
    note = (
        f"{counts.silent_events} silent-while-playing + {counts.stalled_events} presentation-stalled events "
        f"over {_minutes(counts.playing_ms)} playing minutes"
    )
    return build_row(
        kpi=SILENT_KPI,
        value=_per_hour(events, counts.playing_ms),
        unit=S1_UNIT,
        method=SILENT_METHOD,
        meta=meta,
        note=note,
    )


def samples_to_ledger_rows(samples: list[dict[str, Any]], meta: CaptureMeta) -> list[dict[str, Any]]:
    """The two S1 ledger rows from client performance samples."""
    counts = tally(samples)
    return [xrun_row(counts, meta), silent_row(counts, meta)]


def main(argv: list[str] | None = None) -> int:
    """Print the S1 rows for a samples file; never appends to the ledger."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", type=Path, required=True, help="client performance samples (JSON or JSONL)")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the rows; appending to the ledger is the live capture's job"
    )
    parser.add_argument("--machine", default="unknown")
    parser.add_argument("--sha", default="unknown")
    args = parser.parse_args(argv)
    if not args.dry_run:
        parser.error("only --dry-run is supported: this aggregator prints rows and never appends to the ledger")
    now = datetime.now(UTC)
    meta = CaptureMeta(
        capture_id=f"s1-agg-{now:%Y%m%dT%H%M%SZ}", date=now.date().isoformat(), machine=args.machine, sha=args.sha
    )
    samples = [
        sample
        for sample in load_samples(args.samples)
        if sample.get("kind", "client-performance-sample") == "client-performance-sample"
    ]
    for row in samples_to_ledger_rows(samples, meta):
        print(json.dumps(row, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
