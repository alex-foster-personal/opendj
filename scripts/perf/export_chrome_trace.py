#!/usr/bin/env -S uv run --no-project --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Merge every openDJ perf layer into ONE Chrome JSON Trace Event file.

This is the allstack view the program spec asks for (section 2, "export: Chrome
JSON Trace Event format -> ui.perfetto.dev"). Today the frontend stage map and
the native process counters live in two formats nothing reads together, so "the
deck load got slow" and "the engine was at 90% CPU" are two separate
investigations. One trace puts them on one timeline.

Sources
-------
frontend  the `mdt.perfEventLog` ring, read either from a live WKWebView
          localStorage sqlite (`--localstorage`) or a JSON dump
          (`--perf-log`). Deck loads become nested complete events, one span
          per phase, on a per-deck track.
native    the diagnostics probe JSONL (`--probe`). Per-role CPU percent and
          physical footprint become counter tracks.

The adapters that build those events, and their shared timebase, live in
`perf_trace_sources.py`. This file owns the MERGE: one origin, names, and
the honesty check on whether the two layers describe one session at all.

FILE REQUIREMENTS (mini-PRD)

* R1 the output loads in ui.perfetto.dev / chrome://tracing.
  Status: OK, run, works as expected.
  - if the top level were a bare array with no process metadata then Perfetto
    shows unnamed tracks
  - if the two layers kept separate zero points then they never line up
  - if there are no timestamped events at all then the export refuses rather
    than writing an empty file that reads as "nothing was slow"
* R2 the report states what came from each source.
  Status: OK, run, works as expected.
  - if the probe log is absent then the count is 0 and the reason is printed,
    so an empty native layer is never mistaken for a quiet machine
  - if a stage name has no declared meaning then it is named, not dropped
* R3 layers that do not overlap in time say so.
  Status: OK, run, works as expected.
  - if a weeks-old 40-row ring is merged with today's probe log then the trace
    spans those weeks and the counters explain none of the loads
  - if a probe sample falls DURING a load then that is an overlap, not a gap

Usage
-----
    uv run scripts/perf/export_chrome_trace.py --find-localstorage
    uv run scripts/perf/export_chrome_trace.py \\
        --localstorage ~/Library/WebKit/com.opendj.desktop/.../localstorage.sqlite3 \\
        --probe ~/.local/share/music-dj-tools/performance/opendj-performance-2026-09-01.jsonl
    open https://ui.perfetto.dev   # then drag the printed file in
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    # Direct invocation (`uv run --no-project --script export_chrome_trace.py`,
    # or its PEP 723 shebang) puts this file's own directory on sys.path, not
    # the repo root, so the package-qualified import below cannot resolve.
    # `python -m scripts.perf.export_chrome_trace` puts the repo root on
    # sys.path instead, where only the package-qualified form resolves. Both
    # entry points are documented usage, so both imports have to work.
    from scripts.perf import perf_log_model as _plm
    from scripts.perf import perf_trace_sources as _pts
    from scripts.perf import trace_overlap as _to
except ImportError:
    import perf_log_model as _plm  # type: ignore[import-not-found,no-redef]
    import perf_trace_sources as _pts  # type: ignore[import-not-found,no-redef]
    import trace_overlap as _to  # type: ignore[import-not-found,no-redef]

PerfLogUnreadable = _plm.PerfLogUnreadable
event_kind_family = _plm.event_kind_family
find_localstorage_stores = _plm.find_localstorage_stores
read_json_perf_log = _plm.read_json_perf_log
read_localstorage_perf_log = _plm.read_localstorage_perf_log

FRONTEND_PID = _pts.FRONTEND_PID
NATIVE_PID = _pts.NATIVE_PID
NO_DECK_TID = _pts.NO_DECK_TID
PROBE_LOG_DIRS = _pts.PROBE_LOG_DIRS
TraceRefused = _pts.TraceRefused
frontend_events = _pts.frontend_events
newest_probe_log = _pts.newest_probe_log
probe_events = _pts.probe_events

_closest_approach_us = _to._closest_approach_us
_load_path_events = _to._load_path_events
_merged_spans_us = _to._merged_spans_us
_sampled_spans_us = _to._sampled_spans_us
_window_us = _to._window_us
source_overlap = _to.source_overlap

# ------------------------------------------------------------------ build


def _metadata_events(deck_tids: set[int]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        {
            "name": "process_name",
            "ph": "M",
            "pid": FRONTEND_PID,
            "tid": NO_DECK_TID,
            "args": {"name": "frontend (WKWebView ring)"},
        },
        {
            "name": "process_name",
            "ph": "M",
            "pid": NATIVE_PID,
            "tid": NO_DECK_TID,
            "args": {"name": "native (diagnostics probe)"},
        },
    ]
    events.extend(
        {
            "name": "thread_name",
            "ph": "M",
            "pid": FRONTEND_PID,
            "tid": tid,
            "args": {"name": "no deck" if tid == NO_DECK_TID else f"deck {tid}"},
        }
        for tid in sorted(deck_tids)
    )
    return events


def build_trace(
    frontend: list[dict[str, Any]], native: list[dict[str, Any]]
) -> dict[str, Any]:
    """Normalize both streams onto one origin and wrap them in the object form.

    The object form (rather than a bare array) is what carries process and
    thread names, without which Perfetto renders unlabeled tracks.
    """

    timed = [event for event in frontend + native if "ts" in event]
    if not timed:
        raise TraceRefused(
            "no timestamped events from any source, so there is nothing to trace"
        )
    origin_us = min(event["ts"] for event in timed)
    for event in timed:
        event["ts"] -= origin_us
    deck_tids = {event["tid"] for event in frontend if "tid" in event}
    return {
        "displayTimeUnit": "ms",
        "traceEvents": _metadata_events(deck_tids) + frontend + native,
        "otherData": {
            "producer": "scripts/perf/export_chrome_trace.py",
            "trace_origin_epoch_us": origin_us,
            "trace_origin_utc": datetime.fromtimestamp(
                origin_us / 1_000_000, tz=UTC
            ).isoformat(),
            "timebase": (
                "both sources stamp UTC wall clock; a deck-load span starts at "
                "row.t minus stages.total"
            ),
        },
    }


# -------------------------------------------------------------------- cli


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--localstorage", type=Path, help="WKWebView localstorage.sqlite3")
    parser.add_argument("--perf-log", type=Path, help="JSON dump of the ring")
    parser.add_argument(
        "--probe",
        type=Path,
        default=None,
        help="diagnostics probe JSONL; unset auto-discovers the newest one",
    )
    parser.add_argument("--no-probe", action="store_true", help="frontend layer only")
    parser.add_argument("--out", type=Path, default=None, help="output trace path")
    parser.add_argument(
        "--find-localstorage",
        action="store_true",
        help="list WebKit localStorage stores holding a perf ring, then exit",
    )
    parser.add_argument(
        "--webkit-root",
        type=Path,
        default=Path.home() / "Library/WebKit",
        help="root searched by --find-localstorage",
    )
    return parser.parse_args(argv)


def _find_and_report(root: Path) -> int:
    stores = find_localstorage_stores(root)
    if not stores:
        print(f"[ERROR] no localStorage stores under {root}", file=sys.stderr)
        return 3
    print(f"[OK] {len(stores)} stores under {root}, newest first. Ones with a ring:")
    found = 0
    for store in stores:
        try:
            rows = read_localstorage_perf_log(store)
        except PerfLogUnreadable:
            continue
        found += 1
        kinds = sorted({event_kind_family(row["kind"]) for row in rows})
        print(f"  {len(rows):4d} rows  {','.join(kinds)}")
        print(f"          {store}")
    if found == 0:
        print(
            "[ERROR] none of them hold a perf ring. Open the app, load a deck, "
            "then re-run.",
            file=sys.stderr,
        )
        return 4
    return 0


def _load_frontend(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict]:
    if args.localstorage is not None:
        return frontend_events(
            read_localstorage_perf_log(args.localstorage), str(args.localstorage)
        )
    return frontend_events(read_json_perf_log(args.perf_log), str(args.perf_log))


def _load_native(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict]:
    """The native layer, or an empty one with the reason printed.

    An absent probe log is NOT an error: the frontend layer alone is useful.
    It is loud, though -- an empty counter track otherwise reads as a machine
    doing nothing."""

    empty: dict[str, Any] = {"path": None, "records": 0}
    if args.no_probe:
        return [], empty
    if args.probe is not None:
        if not args.probe.exists():
            raise TraceRefused(f"no probe log at {args.probe}")
        return probe_events(args.probe)
    discovered = newest_probe_log()
    if discovered is None:
        print(
            "[WARN] no diagnostics probe JSONL found in "
            + " or ".join(str(directory) for directory in PROBE_LOG_DIRS)
            + ". The native counter layer will be EMPTY -- that is a missing "
            "probe, not a quiet machine. Install it with `just probe-install`, "
            "or generate one with `python3 -m "
            "scripts.diagnostics.opendj_performance_probe --max-samples 20 "
            "--output-dir .tmp/perf/probe`.",
            file=sys.stderr,
        )
        return [], empty
    return probe_events(discovered)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.find_localstorage:
        return _find_and_report(args.webkit_root)
    if (args.localstorage is None) == (args.perf_log is None):
        print(
            "[ERROR] pass exactly one frontend source: --localstorage or --perf-log "
            "(--find-localstorage lists the candidates)",
            file=sys.stderr,
        )
        return 2

    try:
        frontend, frontend_report = _load_frontend(args)
        native, native_report = _load_native(args)
        # Computed BEFORE build_trace, which rebases every ts onto the trace
        # origin and would leave nothing to compare wall clocks with.
        overlap = source_overlap(frontend, native)
        trace = build_trace(frontend, native)
        trace["otherData"]["source_overlap"] = overlap
    except (PerfLogUnreadable, TraceRefused) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 5

    out = args.out
    if out is None:
        # Microseconds, not just seconds: two runs finishing in the same
        # second would otherwise pick the identical default dir and the
        # later `write_text()` silently overwrites the earlier one's trace
        # (Codex P2, #705, export_chrome_trace.py:515).
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        out = Path.cwd() / ".tmp/perf" / f"{stamp}-trace" / "opendj-trace.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(trace, indent=1), encoding="utf-8")

    print(f"[OK] wrote {out} ({out.stat().st_size} bytes)")
    print(f"[OK] traceEvents: {len(trace['traceEvents'])} total")
    print(
        "[OK] frontend: {rows} ring rows -> {spanned} spanned loads, {stems} stem upgrades, "
        "{failed} failed spans, {instant} instant rows".format(
            rows=frontend_report["rows"],
            spanned=frontend_report["spanned_loads"],
            stems=frontend_report["spanned_stem_upgrades"],
            failed=frontend_report["failed_load_spans"],
            instant=frontend_report["instant_rows"],
        )
    )
    if frontend_report["unclassified_stages"]:
        print(
            "[WARN] stage names with no declared meaning in perf_log_model.py: "
            + ", ".join(frontend_report["unclassified_stages"])
            + " (reported, not dropped; add them to the model)"
        )
    if frontend_report["clamped_negative_durations"]:
        print(
            f"[WARN] {frontend_report['clamped_negative_durations']} negative "
            "phase durations were clamped to 0; the phase model disagrees with "
            "the measured totals"
        )
    if frontend_report["negative_residual_loads_phases_skipped"]:
        print(
            f"[WARN] {frontend_report['negative_residual_loads_phases_skipped']} "
            "of the spanned loads above have NO phase breakdown in the trace: "
            "their phases overshot the load's own total, so emitting them would "
            "have placed child spans outside their parent. The top-level load "
            "span is still present; only its children were withheld."
        )
    if frontend_report.get("unclassified_phase_loads_skipped"):
        print(
            f"[WARN] {frontend_report['unclassified_phase_loads_skipped']} "
            "of the spanned loads above have NO phase breakdown either: they "
            "carry stage names this model cannot place on a deck-load row (a "
            "pre-LAZY-STEMS build timed its stem work inline), so drawing "
            "phases would fold that work into a false serial gap. The "
            "top-level load span is still present; only its children were "
            "withheld."
        )
    print(
        "[OK] native: {records} probe records ({samples} samples) from {path}".format(
            records=native_report.get("records", 0),
            samples=native_report.get("sample_records", 0),
            path=native_report.get("path"),
        )
    )
    if native_report.get("withheld_unmeasured_values"):
        print(
            f"[OK] withheld {native_report['withheld_unmeasured_values']} null "
            "counter values (the probe's first sample has no prior to difference "
            "against; reported as absent, never as zero)"
        )
    overlap_warning = _overlap_warning(frontend_report, native_report, overlap)
    if overlap_warning:
        print(overlap_warning)
    print(f"[OK] open https://ui.perfetto.dev and drag in {out}")
    return 0


def _overlap_warning(
    frontend_report: dict[str, Any], native_report: dict[str, Any], overlap: dict[str, Any]
) -> str | None:
    """Which (if any) native/frontend comparability warning main() should print.

    An error-only probe log yields zero-width `probe-error` instants, so
    `comparable`/`overlaps` can both read True with zero real samples behind
    them -- gate on the sample count directly (Codex P2, #705,
    discussion_r3914647495, AGENTS.md L75-78).
    """

    path = native_report.get("path")
    if overlap.get("comparable") and path is not None and not native_report.get("sample_records"):
        return (
            f"[WARN] native layer contributed 0 samples from {path}: "
            f"{native_report.get('records', 0)} probe records were read but none "
            "carried process data, so there is no native measurement to compare "
            "against the ring."
        )
    if overlap.get("comparable") and not overlap["overlaps"]:
        return (
            f"[WARN] the two layers do NOT overlap in time: a {overlap['gap_seconds']:.0f}s "
            f"gap between the ring ({overlap['frontend_utc'][0]} .. "
            f"{overlap['frontend_utc'][1]}) and the probe log "
            f"({overlap['native_utc'][0]} .. {overlap['native_utc'][1]}). "
            f"Measured against {overlap['compared']}. The counters describe a "
            "DIFFERENT session from them. The trace is still correct, but do "
            "not read one layer as an explanation of the other."
        )
    if not overlap.get("comparable") and path is not None:
        # Read but empty: name whichever layer actually contributed nothing,
        # using the counts already gathered for the [OK] lines above -- a
        # generic "confirm the app ran" sends the reader at the wrong
        # (healthy) layer when only one side is actually empty (Codex P2,
        # #705, export_chrome_trace.py:588).
        native_empty = not native_report.get("sample_records")
        frontend_empty = not frontend_report.get("rows")
        if native_empty and not frontend_empty:
            empty_layer = f"the probe log at {path} contributed no usable samples"
        elif frontend_empty and not native_empty:
            empty_layer = "the frontend ring contributed no events"
        else:
            empty_layer = (
                "neither the frontend ring nor the probe log at "
                f"{path} contributed usable events"
            )
        return (
            f"[WARN] native and frontend layers are NOT comparable: {empty_layer} -- "
            "confirm the app was actually running while it captured."
        )
    return None


if __name__ == "__main__":
    raise SystemExit(main())
