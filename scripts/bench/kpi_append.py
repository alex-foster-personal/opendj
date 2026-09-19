# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Append a KPI snapshot to kpi_ledger.json and print the KPIs-by-snapshot table.

Usage:
    uv run scripts/bench/kpi_append.py --label <run-label> [--note "commentary"]
        [--window latest|all] [--since ISO] [--until ISO]
        [--set si_sdr_db=20.35 ...] [--claim per_track_wall_s=7.1] [--no-derive]

DERIVED BY DEFAULT. Every speed, cost, concurrency and throughput field is
recomputed from per-track cache telemetry by kpi_derive; nobody types them.
--set is only for KPIs that genuinely cannot be derived (si_sdr_db, region_iou,
human_quality_1to10, si_sdr_true_db, fixed_overhead_s, ops_incidents_per_run,
the iter_latency pair). Passing --set for a derived key is a HARD ERROR naming
--claim, which records the hand figure next to the derived one as a
claimed-vs-derived pair so a wrong number is visible rather than authoritative.

Snapshots record their own provenance: `provenance[key]` is "derived", "hand",
"configured-not-measured", "not-derivable" or "unmeasured", and `derivation`
carries the full telemetry payload (window, tracks measured, concurrency
basis). A reader can always tell which numbers were measured.

--no-derive is the escape hatch for a snapshot with no telemetry behind it (a
quality-only run scored offline). It marks every derivable key "unmeasured"
rather than inventing a value.

Acceptance:
- [if] a --set names a key absent from kpis [then] exit non-zero listing valid keys.
- [if] a --set names a DERIVED key [then] exit non-zero pointing at --claim.
- [if] a --set or --claim value is not a number [then] exit non-zero naming the pair.
- [if] derivation runs [then] every key in DERIVED_LEDGER_KEYS is filled from
  telemetry and marked "derived" in provenance.
- [if] --no-derive is passed [then] derivable keys are null and marked "unmeasured".
- [if] --claim disagrees with the derived value [then] both are kept and the
  delta prints to stdout.
- [if] the derived window ends before the previous snapshot's ts [then] exit
  non-zero rather than attaching stale telemetry to a new run.
- [if] --note is omitted [then] the snapshot's "notes" field is null, never "".
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

# Dual-mode: run by path (`uv run scripts/bench/kpi_append.py`), sys.path[0] is
# this directory and the sibling import is the right one; imported as
# `scripts.bench.kpi_append` (tests), the relative one is. No sys.path surgery.
if __package__:
    from .kpi_derive import DERIVED_LEDGER_KEYS, derive_window, ledger_values
else:
    from kpi_derive import DERIVED_LEDGER_KEYS, derive_window, ledger_values

LEDGER_PATH = Path(__file__).with_name("kpi_ledger.json")

# Written from a config constant, never measured. Kept only so the historical
# snapshots stay readable; new snapshots leave it null and use
# observed_{mean,peak}_concurrency instead.
CONFIGURED_ONLY_KEYS: frozenset[str] = frozenset({"max_parallel_gpus"})


#----- io + validation ------------------------------------------------------


def _load_ledger(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"[ERROR] ledger not found: {path}")
    ledger = json.loads(path.read_text())
    if "kpis" not in ledger or "snapshots" not in ledger:
        sys.exit(f"[ERROR] {path} missing 'kpis' or 'snapshots' key")
    return ledger


def _parse_pairs(pairs: list[str], valid_keys: list[str], flag: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for pair in pairs:
        if "=" not in pair:
            sys.exit(f"[ERROR] {flag} expects key=value, got: {pair!r}")
        key, raw = pair.split("=", 1)
        key = key.strip()
        if key not in valid_keys:
            sys.exit(f"[ERROR] unknown KPI key {key!r}. Valid keys: {', '.join(valid_keys)}")
        try:
            values[key] = float(raw)
        except ValueError:
            sys.exit(f"[ERROR] value for {key!r} is not a number: {raw!r}")
    return values


def _reject_derivable_sets(set_values: dict[str, float]) -> None:
    clashes = sorted(set_values.keys() & DERIVED_LEDGER_KEYS.keys())
    if not clashes:
        return
    sys.exit(
        f"[ERROR] {', '.join(clashes)} is derived from per-track cache telemetry and "
        "cannot be hand-set.\n"
        "  Drop the --set and let it derive, or record the hand figure as a "
        "claimed-vs-derived pair:\n"
        f"    --claim {clashes[0]}=<value>"
    )


#----- table render ---------------------------------------------------------


def _fmt(value: float | None) -> str:
    if value is None:
        return "null"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def _render_table(ledger: dict) -> str:
    kpis, snapshots = ledger["kpis"], ledger["snapshots"]
    key_w = max([len("KPI")] + [len(k) for k in kpis])
    col_w = [max(len(s["label"]), 8) for s in snapshots]

    def row(cells: list[str]) -> str:
        body = "  ".join(c.rjust(col_w[i]) for i, c in enumerate(cells[1:]))
        return f"{cells[0].ljust(key_w)}  {body}"

    lines = [
        row(["KPI"] + [s["label"] for s in snapshots]),
        "-" * key_w + "  " + "  ".join("-" * w for w in col_w),
    ]
    for key in kpis:
        lines.append(row([key] + [_fmt(s["values"].get(key)) for s in snapshots]))
    return "\n".join(lines)


#----- main -----------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Append a KPI snapshot to kpi_ledger.json.")
    parser.add_argument("--label", required=True, help="short run label for the snapshot column")
    parser.add_argument("--set", dest="sets", action="append", default=[], metavar="key=value",
                        help="set one NON-derivable KPI; repeatable")
    parser.add_argument("--claim", dest="claims", action="append", default=[], metavar="key=value",
                        help="record a hand figure next to the derived one; repeatable")
    parser.add_argument("--note", default=None, help="author commentary shown in the admin panel")
    parser.add_argument("--no-derive", dest="derive", action="store_false",
                        help="snapshot has no telemetry behind it; mark derivable keys unmeasured")
    parser.add_argument("--window", default="latest", choices=["latest", "all"],
                        help="telemetry window to derive from")
    parser.add_argument("--since", default=None, help="ISO 8601 lower bound on publication time")
    parser.add_argument("--until", default=None, help="ISO 8601 upper bound on publication time")
    parser.add_argument("--allow-stale", action="store_true",
                        help="permit telemetry older than the previous snapshot")
    args = parser.parse_args()

    ledger = _load_ledger(LEDGER_PATH)
    valid_keys = list(ledger["kpis"].keys())
    set_values = _parse_pairs(args.sets, valid_keys, "--set")
    claim_values = _parse_pairs(args.claims, valid_keys, "--claim")
    _reject_derivable_sets(set_values)

    values: dict[str, float | None] = {key: set_values.get(key) for key in valid_keys}
    provenance = {
        key: "hand" if key in set_values
        else "configured-not-measured" if key in CONFIGURED_ONLY_KEYS
        else "unmeasured"
        for key in valid_keys
    }
    derivation: dict | None = None

    if args.derive:
        derived = derive_window(args.window, args.since, args.until)
        if ledger["snapshots"] and not args.allow_stale:
            previous_ts = ledger["snapshots"][-1]["ts"]
            if derived.window_end < previous_ts:
                sys.exit(
                    f"[ERROR] derived telemetry ends {derived.window_end}, before the previous "
                    f"snapshot at {previous_ts}: this run wrote no cache entries.\n"
                    "  Use --no-derive for a snapshot with no telemetry, or --since/--until "
                    "to name the window, or --allow-stale if the overlap is intended."
                )
        derivation = asdict(derived)
        for key, value in ledger_values(derived).items():
            if key not in valid_keys:
                sys.exit(f"[ERROR] derived key {key!r} is not in the ledger's kpis dict")
            values[key] = value
            provenance[key] = "derived" if value is not None else "not-derivable"

    snapshot = {
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label": args.label,
        "values": values,
        "provenance": provenance,
        "derivation": derivation,
        "notes": args.note,
    }
    if claim_values:
        snapshot["claims"] = claim_values
    ledger["snapshots"].append(snapshot)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2) + "\n")

    print(f"[OK] appended snapshot {args.label!r} ({snapshot['ts']})")
    if derivation is not None:
        print(f"[OK] derived {len(DERIVED_LEDGER_KEYS)} key(s) from "
              f"{derivation['tracks_measured']} cache entries, concurrency basis "
              f"{derivation['concurrency_basis']}")
    for key, claimed in claim_values.items():
        derived_value = values.get(key)
        if derived_value is None:
            print(f"[WARN] claim {key}={claimed} has no derived counterpart to check against")
        elif abs(claimed - derived_value) > 1e-9:
            print(f"[WARN] claim {key}={claimed} disagrees with derived {derived_value} "
                  f"(delta {claimed - derived_value:+.4g}); the derived value is what renders")
    print(_render_table(ledger))


if __name__ == "__main__":
    main()
