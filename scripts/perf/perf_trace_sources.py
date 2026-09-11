"""Turn each openDJ perf SOURCE into Chrome Trace Event objects.

One adapter per layer. Split out of `export_chrome_trace.py` so that reading a
source and merging the sources are separate concerns: the merge cares only
about events with timestamps, and knows nothing about ring rows or probe
records.

TIMEBASE (the assumption both adapters make, stated once)
---------------------------------------------------------
Both sources stamp UTC ISO 8601 off the same wall clock: the ring writes
`new Date().toISOString()` inside `recordPerfTiming`, and the probe writes
`datetime.now(timezone.utc)`. They therefore share a real timeline with NO
correlation step, and both convert to microseconds since the epoch.

The one DERIVED quantity is a load's START. A ring row is stamped when the load
FINISHES (`recordDeckLoadTiming` is called right after `stages.total` is
computed), so a load's start is taken as `t - total` and its phases are laid out
from there in the order `load()` awaits them. A `deck-load-fail` or
`deck-stems-fail` row never reaches `total`, but walks back through `failedAt`
instead -- a real measured checkpoint from the same `perfT0`, not an invented
one. A row with neither key has no duration to walk back through and becomes
an instant event at `t`, never a span with an invented start.

A timestamp that cannot be parsed aborts the export. An event placed at a
fabricated time is worse than a trace that failed to build.

FILE REQUIREMENTS (mini-PRD)

* R1 non-duration stage keys never become spans. Status: OK, run, works as
  expected.
  - if `audioBytes` (5,059,178) were emitted as microseconds then one load
    draws an 84-minute span across the whole trace
  - if `stemmed` (0 or 1) became a span then a 1ms artifact appears per load
  - the values are still carried, as `args`, so nothing is lost
* R2 a source with no usable timestamp refuses rather than guessing.
  Status: OK, run, works as expected.
  - if a ring row has no `t` then the export exits non-zero naming the row
  - if a probe record has no `timestamp` then the export exits non-zero
  - if a stamp is naive then it is refused, not assumed to be UTC
* R3 an unmeasured counter value is withheld, never drawn as zero.
  Status: OK, run, works as expected.
  - if the probe's first sample (null `cpu_percent`, no prior to difference
    against) were coerced to 0.0 then it is indistinguishable from the ~0.03%
    an idle engine really reads
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from scripts.perf.perf_log_model import (
        NON_DURATION_STAGES,
        deck_load_phase_model,
        epoch_us,
        event_kind_family,
        stem_phase_model,
        unclassified_deck_stages,
    )
except ImportError:
    # Loaded as a bare sibling module when export_chrome_trace.py runs
    # directly (its own directory on sys.path, not the repo root); see the
    # matching comment in export_chrome_trace.py.
    from perf_log_model import (  # type: ignore[import-not-found,no-redef]
        NON_DURATION_STAGES,
        deck_load_phase_model,
        epoch_us,
        event_kind_family,
        stem_phase_model,
        unclassified_deck_stages,
    )


class TraceRefused(RuntimeError):
    """The trace cannot be built honestly, so it is not built at all."""


FRONTEND_PID = 1
NATIVE_PID = 2
NO_DECK_TID = 0

MATERIAL_RESIDUAL_MS = 0.5
"""Below this, a nonzero `residual_ms` is stage rounding, not a broken model.

Matches the threshold `amdahl_report.py` uses for `nonzero_residual_loads`,
so the two tools agree on what counts as "the phases do not really re-add to
the total"."""

# Where the probe writes, in the two places it can be configured to write.
# probe_types.DEFAULT_OUTPUT_DIR is what a bare `-m ...probe` uses; the
# justfile's launchd install overrides it with the Application Support path.
PROBE_LOG_DIRS = (
    Path.home() / "Library/Application Support/OpenDJ Diagnostics/performance",
    Path.home() / ".local/share/music-dj-tools/performance",
)


# --------------------------------------------------------------- frontend


def _stage_facts(stages: dict[str, Any]) -> dict[str, Any]:
    """The non-duration entries, carried as args so nothing is lost."""

    return {name: stages[name] for name in NON_DURATION_STAGES if name in stages}


def _complete_event(
    name: str, category: str, start_us: int, duration_us: int, tid: int, args: dict
) -> dict[str, Any]:
    return {
        "name": name,
        "cat": category,
        "ph": "X",
        "ts": start_us,
        "dur": max(0, duration_us),
        "pid": FRONTEND_PID,
        "tid": tid,
        "args": args,
    }


def frontend_events(rows: list[dict[str, Any]], origin: str) -> tuple[list[dict], dict]:
    """Ring rows as trace events, plus a report of what was and was not modeled."""

    events: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "rows": len(rows),
        "spanned_loads": 0,
        "spanned_stem_upgrades": 0,
        "failed_load_spans": 0,
        "instant_rows": 0,
        "clamped_negative_durations": 0,
        "negative_residual_loads_phases_skipped": 0,
        "unclassified_phase_loads_skipped": 0,
        "unclassified_stages": set(),
    }
    for index, row in enumerate(rows):
        stamp = row.get("t")
        if not isinstance(stamp, str):
            raise TraceRefused(
                f"{origin}: ring row {index} ({row.get('kind')!r}) has no string `t`. "
                "Its events cannot be placed on a timeline and will not be invented."
            )
        end_us = epoch_us(stamp, f"{origin} row {index}")
        deck = row.get("deck")
        tid = deck if isinstance(deck, int) and 1 <= deck <= 4 else NO_DECK_TID
        stages = row.get("stages")
        family = event_kind_family(row["kind"])

        if not isinstance(stages, dict):
            events.append(
                {
                    "name": family,
                    "cat": "perf-event",
                    "ph": "i",
                    "s": "t",
                    "ts": end_us,
                    "pid": FRONTEND_PID,
                    "tid": tid,
                    "args": {"message": row.get("message"), "id": row.get("id")},
                }
            )
            report["instant_rows"] += 1
            continue

        is_load_path = family.startswith(("deck-load", "deck-stems"))
        row_unclassified: set[str] = set()
        if is_load_path:
            row_unclassified = unclassified_deck_stages(
                stages, is_load=family.startswith("deck-load")
            )
            report["unclassified_stages"] |= row_unclassified
        model = (
            stem_phase_model(stages)
            if family.startswith("deck-stems")
            else deck_load_phase_model(stages)
            if family.startswith("deck-load")
            else None
        )
        if model is None:
            # Neither phase model can lay out phases without `total`, but a
            # `deck-load-fail` or `deck-stems-fail` row never reaches it -- it
            # stops at `failedAt`, a real measured checkpoint from the same
            # `perfT0`, not an invented one. Dropping straight to an instant
            # event throws that duration away, hiding the interval a native
            # counter overlap could otherwise explain (Codex P1/BLOCKING,
            # #705, perf_trace_sources.py:190).
            failed_at = stages.get("failedAt")
            if is_load_path and isinstance(failed_at, (int, float)):
                start_us = end_us - round(float(failed_at) * 1000)
                events.append(
                    _complete_event(
                        row["kind"],
                        family,
                        start_us,
                        round(float(failed_at) * 1000),
                        tid,
                        {"stages": stages, "labels": row.get("labels")},
                    )
                )
                report["failed_load_spans"] += 1
                continue
            events.append(
                {
                    "name": family,
                    "cat": "perf-timing",
                    "ph": "i",
                    "s": "t",
                    "ts": end_us,
                    "pid": FRONTEND_PID,
                    "tid": tid,
                    "args": {"stages": stages, "labels": row.get("labels")},
                }
            )
            report["instant_rows"] += 1
            continue

        start_us = end_us - round(model.total_ms * 1000)
        events.append(
            _complete_event(
                row["kind"],
                family,
                start_us,
                round(model.total_ms * 1000),
                tid,
                {
                    "facts": _stage_facts(stages),
                    "labels": row.get("labels"),
                    "residual_ms": round(model.residual_ms, 2),
                    "stages_ms": {
                        k: v for k, v in stages.items() if k not in NON_DURATION_STAGES
                    },
                },
            )
        )
        if model.residual_ms < -MATERIAL_RESIDUAL_MS:
            # The phases OVERSHOOT the load's own total (e.g. 5ms fetch +
            # 5ms decode inside a 9ms load): emitting them anyway would
            # advance the cursor past `end_us` and place child spans OUTSIDE
            # their parent in the trace, with nothing in the trace itself
            # saying so. Reject the phase breakdown for this one load rather
            # than export spans the model itself says are wrong; the
            # top-level complete event above still records that the load
            # happened. (.claude/rules/verification.md: a failed measurement
            # must not render as a verdict.)
            report["negative_residual_loads_phases_skipped"] += 1
            report["spanned_loads"] += 1
            continue
        if row_unclassified:
            # A pre-LAZY-STEMS deck-load row carrying stem work inline: the
            # model still classified `total`, but the phases it can build
            # fold that work into `unattributed-pre-swap`, which would draw
            # a false serial span. The complete event above already recorded
            # the load happened; only the (wrong) phase breakdown is
            # withheld, same treatment as a negative residual.
            report["unclassified_phase_loads_skipped"] += 1
            report["spanned_loads"] += 1
            continue

        cursor = start_us
        for phase in model.phases:
            duration_us = round(phase.wall_ms * 1000)
            if duration_us < 0:
                report["clamped_negative_durations"] += 1
            events.append(
                _complete_event(
                    phase.name,
                    f"{family}.phase",
                    cursor,
                    duration_us,
                    tid,
                    {
                        "wall_ms": phase.wall_ms,
                        "parallelizable": phase.parallelizable,
                        "why": phase.why,
                    },
                )
            )
            cursor += max(0, duration_us)
        report["spanned_loads" if family.startswith("deck-load") else "spanned_stem_upgrades"] += 1

    report["unclassified_stages"] = sorted(report["unclassified_stages"])
    return events, report


# ----------------------------------------------------------------- native


def newest_probe_log() -> Path | None:
    """The most recently written probe JSONL across both configured log dirs."""

    candidates = [
        path
        for directory in PROBE_LOG_DIRS
        if directory.is_dir()
        for path in directory.glob("opendj-performance-*.jsonl")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def probe_events(path: Path) -> tuple[list[dict], dict]:
    """Probe samples as counter tracks, one series per role."""

    events: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "path": str(path),
        "records": 0,
        "sample_records": 0,
        "app_not_running_records": 0,
        "probe_error_records": 0,
        "withheld_unmeasured_values": 0,
    }
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise TraceRefused(f"{path}: line {index + 1} is not JSON: {exc}") from exc
        report["records"] += 1
        stamp = record.get("timestamp")
        if not isinstance(stamp, str):
            raise TraceRefused(
                f"{path}: record {index + 1} (kind {record.get('kind')!r}) has no "
                "string `timestamp`, so it cannot be placed on the timeline."
            )
        ts_us = epoch_us(stamp, f"{path} record {index + 1}")
        kind = record.get("kind")
        if kind == "app-not-running":
            report["app_not_running_records"] += 1
            continue
        if kind == "probe-error":
            report["probe_error_records"] += 1
            events.append(
                {
                    "name": "probe-error",
                    "cat": "probe",
                    "ph": "i",
                    "s": "g",
                    "ts": ts_us,
                    "pid": NATIVE_PID,
                    "tid": NO_DECK_TID,
                    "args": {"error": record.get("error")},
                }
            )
            continue
        processes = record.get("processes")
        if not isinstance(processes, list):
            continue
        counters = _sample_counters(processes, ts_us, report)
        # A shell that exits after family association but before
        # `_family_metrics` finishes can write a `kind: "sample"` record with
        # `processes: []`; counting that row unconditionally reports a usable
        # sample even though `_sample_counters` above emits nothing for it,
        # so `[OK] native: ... (1 samples)` and the comparability warning both
        # read as though the native layer measured something (Codex
        # P2/NON-BLOCKING, #705, perf_trace_sources.py:354). Count a record
        # only when it actually yielded a counter.
        if counters:
            report["sample_records"] += 1
        events.extend(counters)
    return events, report


def _sample_counters(
    processes: list[dict[str, Any]], ts_us: int, report: dict[str, Any]
) -> list[dict[str, Any]]:
    """One counter event per series for one probe sample, roles summed.

    A None value means UNMEASURED, not idle: the probe needs a PRIOR sample to
    difference `cpu_percent` against, so the first record of every run has
    none. Coercing that to 0.0 draws a confident zero for a number nothing
    measured, and an idle engine really does read about 0.03%, so the
    fabrication is indistinguishable from a real reading. Withheld instead,
    and counted, so the omission is visible rather than silent
    (.claude/rules/verification.md).

    A role can have more than one process (two WebKit.WebContent helpers,
    say), and one of them can be unmeasured while its sibling is not -- a
    newly spawned helper has no PRIOR sample to difference against yet, even
    mid-run. Summing only the measured siblings would still publish a
    counter for that role: plausible, and silently missing a member's
    contribution. A role's counter is withheld for a series unless every one
    of its processes was measured (Codex P1/BLOCKING, #705,
    perf_trace_sources.py:359).
    """

    series: dict[str, dict[str, float]] = {"cpu_percent": {}, "footprint_mb": {}}
    partial_roles: dict[str, set[str]] = {"cpu_percent": set(), "footprint_mb": set()}
    for process in processes:
        role = str(process.get("role", "unknown"))
        for key, name in (
            ("cpu_percent", "cpu_percent"),
            ("physical_footprint_mb", "footprint_mb"),
        ):
            value = process.get(key)
            if value is None:
                report["withheld_unmeasured_values"] += 1
                partial_roles[name].add(role)
                continue
            sink = series[name]
            sink[role] = sink.get(role, 0.0) + float(value)
    for name, roles in partial_roles.items():
        for role in roles:
            series[name].pop(role, None)
    return [
        {
            "name": name,
            "cat": "probe",
            "ph": "C",
            "ts": ts_us,
            "pid": NATIVE_PID,
            "tid": NO_DECK_TID,
            "args": values,
        }
        for name, values in series.items()
        if values
    ]
