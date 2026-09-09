#!/usr/bin/env -S uv run --no-project --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""What is the CEILING on a deck load, before anyone builds the optimization.

Every proposal to parallelize part of the load path should be checked against
Amdahl's law first, because the answer is often "the serial part dominates and
this cannot pay". This reads real deck-load stage maps out of the perf ring and
reports, per load and aggregated, how much of the measured wall time is
parallelizable, how much is irreducibly serial, and the best speedup available
at 2, 4 and 8 workers and in the limit.

WHAT COUNTS AS PARALLELIZABLE, AND WHY (the whole argument)
----------------------------------------------------------
Classification lives in `perf_log_model.py`, read off the `time()` call sites:

  fetch-floor/-remainder    Lone fetch member: SERIAL floor plus SERIAL
                            remainder, no parallel split (#705).
  fetch-contested           >1 member: SERIAL, ceiling withheld, no floor (#705).
  fetch-unmeasured          No member breakdown: SERIAL, ceiling withheld (#705).
  decode-mix                SERIAL. One decodeAudioData; WebKit's decode
                            thread bites `decodeStems` (4-way), not this.
  stretchLoad, latency      SERIAL. Awaited one at a time.
  deck-swap, unattributed   SERIAL. Guarded swap and graph setup.
  fetchStems, decodeStems   SERIAL, no per-part floor -- EXCLUDED, not 1.0x (#705).
  stemProcessorCreate       SERIAL. "4 serial worklet creates" (queue Q7).

Phases are DIFFERENCES BETWEEN CHECKPOINTS, never summed member stages:
`fetchWall`/`totalBeforeSwap` are cumulative, and `decodeMix` overlaps
`stretchCreate`, so summing counts the same ms twice.

FILE REQUIREMENTS (mini-PRD) -- all OK, run, work as expected.

* R1 the parallel fraction is derived from wall time, never summed concurrent
  stages. [if fetch members were summed then the fraction exceeds 1.0]
* R2 the denominator is named on every figure (HONEST DENOMINATORS).
  [if 3 of 11 rows are failed loads then the aggregate says "8 of 11"]
* R3 an Amdahl figure is never produced for a load with no phase model.
  [if a load failed before `total` was stamped it is excluded, not zeroed]
* R4 a load whose own phase breakdown does not check out never feeds the
  headline ceiling. [if a stage key is unclassified, or the residual exceeds
  `MATERIAL_RESIDUAL_MS`, the load is anomalous and excluded from
  `aggregate()`; if every load is anomalous the report exits UNKNOWN]
* R5 a contested fetch group (>1 member, single-worker engine) never feeds a
  ceiling, per-load or pooled, but STAYS in the aggregate's total/serial ms,
  named separately as `contested_ms` (#705). [a load with 2+ concurrent fetch
  members prints ceiling UNKNOWN but is NOT excluded from `loads`; if any
  clean load is contested the pooled `ceiling_speedup`/`speedup` are None,
  and `serial_ms`'s print caveat AND JSON both name the unproven `contested_ms`
  slice rather than only the fraction that already charges it as serial]
* R6 a load labeled `stemLayout: pending` is excluded even with zero
  stem-kind rows in the ring (#705). [same load with no `labels` reads as R2]

Usage
-----
    uv run scripts/perf/amdahl_report.py --localstorage <localstorage.sqlite3>
    uv run scripts/perf/amdahl_report.py --perf-log ring.json --json
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    # Direct invocation puts this file's own directory on sys.path, not the
    # repo root; `python -m scripts.perf.amdahl_report` puts the repo root
    # on sys.path instead. Both are documented entry points.
    from scripts.perf import perf_log_model as _plm
except ImportError:
    import perf_log_model as _plm  # type: ignore[import-not-found,no-redef]

FETCH_GROUP_STAGES = _plm.FETCH_GROUP_STAGES
STEM_GROUP_WALL_STAGES = _plm.STEM_GROUP_WALL_STAGES
STEM_KINDS = _plm.STEM_KINDS
PerfLogUnreadable = _plm.PerfLogUnreadable
amdahl_speedup = _plm.amdahl_speedup
deck_load_phase_model = _plm.deck_load_phase_model
event_kind_family = _plm.event_kind_family
load_sid = _plm.load_sid
read_json_perf_log = _plm.read_json_perf_log
read_localstorage_perf_log = _plm.read_localstorage_perf_log
stem_phase_model = _plm.stem_phase_model
unclassified_deck_stages = _plm.unclassified_deck_stages

try:
    from scripts.perf.amdahl_report_print import print_aggregate, print_notes, print_per_load
except ImportError:
    from amdahl_report_print import (  # type: ignore[import-not-found,no-redef]
        print_aggregate,
        print_notes,
        print_per_load,
    )

WORKER_COUNTS = (2.0, 4.0, 8.0)

MATERIAL_RESIDUAL_MS = 0.5
"""Below this, a nonzero residual is stage rounding, not a broken model.
Matches the threshold `perf_trace_sources.py` uses for the same judgment."""


@dataclass
class LoadRecord:
    """One deck load, optionally joined to the stem upgrade that followed it."""

    sid: str | None
    deck: int | None
    kind: str
    total_ms: float
    parallel_ms: float
    residual_ms: float
    fetch_members_ms: dict[str, float] = field(default_factory=dict)
    fetch_wall_ms: float | None = None
    stem_total_ms: float | None = None
    stem_parallel_ms: float | None = None
    anomalous: bool = False
    """Unclassified stage key, material residual, or an unmeasured stem
    ceiling (#705). Excluded from the pooled AND per-load ceiling; still
    shown as UNKNOWN, never a verdict (verification.md)."""
    ceiling_unknown: bool = False
    """Contested fetch group (#705): data is trustworthy, load STAYS in the
    aggregate, but no floor/parallel split exists so only the ceiling
    (per-load and pooled) is withheld -- narrower than `anomalous`."""
    stem_layout_pending: bool = False
    """Row's own `labels.stemLayout` was `pending`: async stem upgrade had
    already started as of write time (#705, amdahl_report.py:300)."""

    @property
    def combined_total_ms(self) -> float:
        """Press to fully stemmed. The stem upgrade runs AFTER load() resolves."""

        return self.total_ms + (self.stem_total_ms or 0.0)

    @property
    def combined_parallel_ms(self) -> float:
        return self.parallel_ms + (self.stem_parallel_ms or 0.0)

    @property
    def parallel_fraction(self) -> float:
        return _fraction(self.combined_parallel_ms, self.combined_total_ms)

    @property
    def fetch_concurrency(self) -> float | None:
        """Sum of the fetch group's members over its wall. 1.0 means no overlap."""

        if not self.fetch_members_ms or not self.fetch_wall_ms:
            return None
        return sum(self.fetch_members_ms.values()) / self.fetch_wall_ms


def _fraction(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    return min(1.0, max(0.0, part / whole))


def _ceiling(parallel_fraction: float) -> float:
    return amdahl_speedup(parallel_fraction, math.inf)


def _normalize_ceiling(ceiling: float | None) -> float | None:
    """An already-computed ceiling, rounded for JSON, or None if infinite
    (fully parallel) or already withheld (`aggregate()` sets None itself for
    a contested fetch group, #705). `json.dumps` emits `math.inf` as the bare
    token `Infinity`, invalid JSON (RFC 8259); null is the honest value.
    Takes the CEILING, not the fraction it came from: recomputing from a
    fraction already rounded for display can cross the 1.0 boundary the raw
    fraction never reached (0.99996 rounds to 1.0000), disagreeing with the
    text report's `ceiling_speedup`, derived from the same raw fraction."""

    return None if ceiling is None or math.isinf(ceiling) else round(ceiling, 3)


def _json_ceiling(parallel_fraction: float) -> float | None:
    """`_normalize_ceiling` computed fresh from a RAW (unrounded) fraction."""

    return _normalize_ceiling(_ceiling(parallel_fraction))


# ------------------------------------------------------------------ build


def _pop_current_awaiting(
    awaiting_stem: dict[tuple[str | None, Any], list[LoadRecord]], key: tuple[str | None, Any]
) -> LoadRecord | None:
    """LIFO pop: the most recent still-unresolved load under `key`. A newer
    load of the same track/deck stales the older upgrade token silently
    (audio-engine.svelte.ts:2851-2855); a superseded entry is left to sweep."""

    queue = awaiting_stem.get(key)
    if not queue:
        return None
    return queue.pop()


@dataclass
class _Join:
    """The mutable state the deck-load / stem join carries across rows.

    One object rather than four parallel arguments: the two row arms below
    each touch a different subset, and threading them individually is how a
    caller ends up passing `orphaned` to the arm that never appends to it."""

    records: list[LoadRecord] = field(default_factory=list)
    awaiting_stem: dict[tuple[str | None, Any], list[LoadRecord]] = field(default_factory=dict)
    orphaned: list[LoadRecord] = field(default_factory=list)
    notes: dict[str, Any] = field(
        default_factory=lambda: {
            "deck_load_rows": 0,
            "incomplete_loads": 0,
            "stem_rows": 0,
            "stem_rows_failed_excluded": 0,
            "stem_rows_no_stems": 0,
            "pending_stem_loads_excluded": 0,
            "loads_with_stems": 0,
            "unclassified_stages": set(),
            "nonzero_residual_loads": 0,
        }
    )


def _ingest_deck_load_row(
    join: _Join, row: dict[str, Any], key: tuple[str | None, Any], family: str
) -> None:
    """One `deck-load` row: model it, record it, and queue it for the stem
    row that may follow. A row the model cannot complete queues nothing and
    clears whatever was already waiting under `key`."""

    notes = join.notes
    stages = row.get("stages")
    if not isinstance(stages, dict):
        return
    notes["deck_load_rows"] += 1
    load_unclassified = unclassified_deck_stages(stages, is_load=True)
    notes["unclassified_stages"] |= load_unclassified
    model = deck_load_phase_model(stages)
    if model is None:
        notes["incomplete_loads"] += 1
        # No LoadRecord for this key, so a later stem row cannot
        # belong to it or whatever was queued behind it: clear the
        # queue rather than let a later pop misattribute a stranger.
        join.orphaned.extend(join.awaiting_stem.pop(key, []))
        return
    residual_material = abs(model.residual_ms) > MATERIAL_RESIDUAL_MS
    if residual_material:
        notes["nonzero_residual_loads"] += 1

    record = LoadRecord(
        sid=key[0],
        deck=row.get("deck"),
        kind=family,
        total_ms=model.total_ms,
        parallel_ms=model.parallelizable_ms,
        residual_ms=model.residual_ms,
        fetch_members_ms={
            name: float(value) for name, value in stages.items() if name in FETCH_GROUP_STAGES
        },
        fetch_wall_ms=float(stages["fetchWall"]) if "fetchWall" in stages else None,
        anomalous=bool(load_unclassified) or residual_material,
        ceiling_unknown=model.contested_fetch_group,
        stem_layout_pending=(
            isinstance(row.get("labels"), dict) and row["labels"].get("stemLayout") == "pending"
        ),
    )
    join.records.append(record)
    join.awaiting_stem.setdefault(key, []).append(record)


def _ingest_stem_row(
    join: _Join, row: dict[str, Any], key: tuple[str | None, Any], family: str
) -> None:
    """One stem-kind row, resolved against the most recent load still
    awaiting one under `key`. Every early return here leaves that load
    either popped-and-marked or untouched, never half-resolved."""

    notes = join.notes
    stages = row.get("stages")
    no_stems = family == "deck-stems-none"
    if family != "deck-stems" and not (no_stems and isinstance(stages, dict)):
        # No usable stage map (legacy row) stays clean mix-only,
        # unlike a real failure (deck-stems-fail).
        notes["stem_rows_no_stems" if no_stems else "stem_rows_failed_excluded"] += 1
        target = _pop_current_awaiting(join.awaiting_stem, key)
        if target is not None and not no_stems:
            target.anomalous = True
        return
    assert isinstance(stages, dict)  # guaranteed by the guard above
    if no_stems:
        # SETTLED but not stageless: probeStem cost real time,
        # modeled as the serial settled tail (Codex P2, #705, :267).
        notes["stem_rows_no_stems"] += 1
    else:
        notes["stem_rows"] += 1
    # Classified the same way regardless of outcome: an unknown stage
    # name means the model does not understand this schema, whether
    # or not stems ended up attached (Codex P2, #705, :292).
    stem_unclassified = unclassified_deck_stages(stages)
    notes["unclassified_stages"] |= stem_unclassified
    target = _pop_current_awaiting(join.awaiting_stem, key)
    if target is None:
        return
    stem_model = stem_phase_model(stages)
    if stem_model is None:
        # `target` is already popped from `awaiting_stem`, so without
        # this it would resolve clean-mix-only, silently dropping an
        # observed but unmodelable stem attempt (Codex P2, #705,
        # amdahl_report.py:297).
        target.anomalous = True
        return
    target.stem_total_ms = stem_model.total_ms
    target.stem_parallel_ms = stem_model.parallelizable_ms
    if bool(stem_unclassified) or abs(stem_model.residual_ms) > MATERIAL_RESIDUAL_MS:
        target.anomalous = True
    if no_stems:
        return
    notes["loads_with_stems"] += 1
    if any(name in stages for name in STEM_GROUP_WALL_STAGES):
        target.anomalous = True  # no per-part floor: not a real ceiling


def build_records(rows: list[dict[str, Any]]) -> tuple[list[LoadRecord], dict[str, Any]]:
    """Deck-load rows as Amdahl records, joined to the stem row that FOLLOWS
    each one, one-to-one, in ring order. A per-key QUEUE (not a single dict
    slot, which would collapse two repeated loads onto whichever stem row
    was written LAST): each `deck-load` row appends to `awaiting_stem[key]`,
    a matching `deck-stems` row pops the most recent (`_pop_current_awaiting`).
    Anything left behind is swept into `pending_stem_loads_excluded`.

    The two row arms live in `_ingest_deck_load_row` and `_ingest_stem_row`.
    They are independent -- neither can see the other's row -- so the only
    thing this loop decides is which one a row belongs to."""

    join = _Join()
    records = join.records
    awaiting_stem = join.awaiting_stem
    orphaned = join.orphaned
    notes = join.notes
    for row in rows:
        family = event_kind_family(row["kind"])
        key = (load_sid(row["kind"]), row.get("deck"))

        if family.startswith("deck-load"):
            _ingest_deck_load_row(join, row, key, family)
        elif family in STEM_KINDS:
            _ingest_stem_row(join, row, key, family)

    # A load still queued (ORPHANED, or SUPERSEDED) when other stem-kind rows
    # resolved never proved "no stem work happened" for itself -- excluded
    # like an observed failure. A ring with ZERO stem-kind rows anywhere (R2)
    # stays mix-only instead -- UNLESS the load's own row said
    # `labels.stemLayout: pending`, direct evidence regardless of the ring (#705:300).
    still_pending = orphaned + [record for queue in awaiting_stem.values() for record in queue]
    stem_kind_rows_seen = (
        notes["stem_rows"] or notes["stem_rows_failed_excluded"] or notes["stem_rows_no_stems"]
    )
    for pending_record in still_pending:
        if stem_kind_rows_seen or pending_record.stem_layout_pending:
            pending_record.anomalous = True
            notes["pending_stem_loads_excluded"] += 1

    notes["unclassified_stages"] = sorted(notes["unclassified_stages"])
    return records, notes


def aggregate(records: list[LoadRecord]) -> dict[str, Any]:
    """Amdahl over the pooled wall time of every complete, TRUSTED load.

    Pooled rather than averaged per-load: averaging fractions weights a 400ms
    load the same as a 3.8s one, but the question is about total wall time
    actually spent. Anomalous loads (a stage map the model does not fully
    trust) are excluded, still visible per-load and counted in
    `anomalous_loads_excluded` so the denominator stays honest."""

    clean = [record for record in records if not record.anomalous]
    if not clean:
        return {"loads": 0, "anomalous_loads_excluded": len(records)}
    total = sum(record.combined_total_ms for record in clean)
    parallel = sum(record.combined_parallel_ms for record in clean)
    fraction = _fraction(parallel, total)
    concurrencies = [r.fetch_concurrency for r in clean if r.fetch_concurrency is not None]
    ceiling_unknown_loads = sum(1 for record in clean if record.ceiling_unknown)
    ceiling_known = ceiling_unknown_loads == 0
    # `serial_ms` below still POOLS this in (R5): a contested load's wall is
    # real ms actually spent, just with no proven split. Naming it separately
    # here is what lets a reader stop reading `serial_fraction` as a verdict
    # over ms this model never measured a floor for (Codex P1/BLOCKING, #705,
    # perf_log_model.py:451 -- following the ceiling-withholding fix one
    # level up, the fraction it feeds was still an unqualified number).
    #
    # Only the FETCH WALL itself lacks a proven split when contested -- the
    # load's other phases (decode, worklet create, deck swap) are already
    # classified as serial. Summing `combined_total_ms` charged the WHOLE
    # load as contested, so the locked mix capture (fetchWall 782ms of a
    # 2,233ms load) reported 2,233ms contested instead of 782 (Codex P2,
    # #705, discussion_r3921310173). `contested_fetch_group` is only ever
    # set inside `deck_load_phase_model`'s `fetch_wall is not None` branch
    # (perf_log_model.py:432), so every `ceiling_unknown` record here has a
    # measured `fetch_wall_ms`.
    contested_records = [record for record in clean if record.ceiling_unknown]
    assert all(record.fetch_wall_ms is not None for record in contested_records), (
        "a ceiling_unknown load has no fetch_wall_ms; contested_fetch_group "
        "can only be set from a load's own fetch wall"
    )
    contested_ms = round(sum(record.fetch_wall_ms or 0.0 for record in contested_records), 1)
    return {
        "loads": len(clean),
        "anomalous_loads_excluded": len(records) - len(clean),
        "ceiling_unknown_loads": ceiling_unknown_loads,
        "contested_ms": contested_ms,
        "total_ms": round(total, 1),
        "parallelizable_ms": round(parallel, 1),
        "serial_ms": round(total - parallel, 1),
        "parallel_fraction": round(fraction, 4),
        "serial_fraction": round(1.0 - fraction, 4),
        "median_load_ms": round(
            statistics.median(record.combined_total_ms for record in clean), 1
        ),
        # None when >=1 clean load is contested: pooling a fraction that
        # already charges unproven time as serial is the same unfounded
        # ceiling Codex flagged, one level up (#705).
        "speedup": (
            {f"N={int(n)}": round(amdahl_speedup(fraction, n), 3) for n in WORKER_COUNTS}
            if ceiling_known
            else None
        ),
        # Raw, possibly-infinite value: `print_aggregate`'s `.3f` prints
        # `inf` cleanly. Only `main()`'s `--json` branch cannot carry
        # `math.inf`, so IT normalizes this field (`_normalize_ceiling`).
        "ceiling_speedup": round(_ceiling(fraction), 3) if ceiling_known else None,
        "floor_ms_if_parallel_were_free": round(total - parallel, 1),
        "median_fetch_concurrency": (
            round(statistics.median(concurrencies), 2) if concurrencies else None
        ),
    }


# -------------------------------------------------------------------- cli


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--localstorage", type=Path, help="WKWebView localstorage.sqlite3")
    parser.add_argument("--perf-log", type=Path, help="JSON dump of the ring")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if (args.localstorage is None) == (args.perf_log is None):
        print(
            "[ERROR] pass exactly one source: --localstorage or --perf-log",
            file=sys.stderr,
        )
        return 2
    try:
        rows = (
            read_localstorage_perf_log(args.localstorage)
            if args.localstorage is not None
            else read_json_perf_log(args.perf_log)
        )
    except PerfLogUnreadable as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 5

    records, notes = build_records(rows)
    if not records:
        print(
            f"[ERROR] UNKNOWN: {len(rows)} ring rows hold no complete deck load "
            f"({notes['deck_load_rows']} deck-load rows, "
            f"{notes['incomplete_loads']} incomplete). Load a deck, then re-run.",
            file=sys.stderr,
        )
        return 4
    summary = aggregate(records)
    if summary["loads"] == 0:
        print(
            f"[ERROR] UNKNOWN: all {summary['anomalous_loads_excluded']} complete "
            "load(s) in this ring are anomalous (an unclassified stage key, a "
            f"residual over {MATERIAL_RESIDUAL_MS}ms, or an unmeasured stem wall) "
            "-- the aggregate ceiling would not be trustworthy. Re-run with a clean ring.",
            file=sys.stderr,
        )
        return 6

    if args.json:
        json_aggregate = {
            **summary,
            # summary["ceiling_speedup"], not "parallel_fraction" recomputed
            # (rounded for display -- see _normalize_ceiling's docstring).
            "ceiling_speedup": _normalize_ceiling(summary["ceiling_speedup"]),
        }
        print(
            json.dumps(
                {
                    "aggregate": json_aggregate,
                    "notes": notes,
                    "loads": [
                        {
                            "deck": record.deck,
                            "sid": record.sid,
                            "total_ms": record.combined_total_ms,
                            "stem_total_ms": record.stem_total_ms,
                            "parallelizable_ms": record.combined_parallel_ms,
                            "parallel_fraction": round(record.parallel_fraction, 4),
                            "ceiling_speedup": (
                                None
                                if record.anomalous or record.ceiling_unknown
                                else _json_ceiling(record.parallel_fraction)
                            ),
                            "fetch_concurrency": record.fetch_concurrency,
                            "residual_ms": record.residual_ms,
                            "anomalous": record.anomalous,
                            "ceiling_unknown": record.ceiling_unknown,
                        }
                        for record in records
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    source = args.localstorage if args.localstorage is not None else args.perf_log
    print(f"Amdahl report for {source}")
    print(f"  {len(rows)} ring rows")
    print_per_load(records)
    print_aggregate(summary, notes)
    print_notes(notes, records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
