# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Append a snapshot to lyrics_kpi_ledger.json and print the trend table.

The lyrics sibling of kpi_append.py (stems). No derivation machinery: lyric
KPIs come from scorer runs (witness-eval, apps.lyrics score, novox_fit) or
live-DB counts, so every value is passed explicitly and STAMPED with its
provenance. 'measured' is a claim that the value was computed this run
against the live scorer/DB; 'hand' is a transcription from the experiment
log. There is no default - an unstated provenance is the failure mode this
script exists to prevent.

Usage:
    uv run scripts/bench/lyrics_kpi_append.py --label <run-label> \
        --provenance measured|hand [--note "commentary"] \
        --set align_median_ms=41 --set align_at100ms_pct=85.2 ...

Acceptance (one-line, house format):
- [if] a --set names a key absent from kpis [then] exit non-zero listing valid keys.
- [if] a --set value is not a number [then] exit non-zero naming the pair.
- [if] --provenance is omitted or not measured|hand [then] exit non-zero.
- [if] ear_vocal_lost is set above 0 [then] append anyway but exit non-zero
  AFTER writing, so the regression is recorded AND the pipeline goes red.
- [if] no --set at all [then] exit non-zero (an empty snapshot is noise).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

LEDGER = Path(__file__).resolve().parent / "lyrics_kpi_ledger.json"


def _parse_sets(pairs: list[str], valid: set[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for pair in pairs:
        if "=" not in pair:
            sys.exit(f"[ERROR] --set needs key=value, got {pair!r}")
        key, _, raw = pair.partition("=")
        if key not in valid:
            sys.exit(f"[ERROR] unknown KPI {key!r}. Valid: {', '.join(sorted(valid))}")
        try:
            out[key] = float(raw)
        except ValueError:
            sys.exit(f"[ERROR] value for {key!r} is not a number: {raw!r}")
    if not out:
        sys.exit("[ERROR] at least one --set is required; an empty snapshot is noise")
    return out


def _trend_table(ledger: dict) -> str:
    keys = list(ledger["kpis"])
    rows = ledger["snapshots"][-6:]
    head = "kpi".ljust(28) + "".join(s["label"][:16].rjust(18) for s in rows)
    lines = [head, "-" * len(head)]
    for key in keys:
        cells = "".join(
            ("-" if s["values"].get(key) is None else f"{s['values'][key]:g}").rjust(18)
            for s in rows
        )
        lines.append(key.ljust(28) + cells)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", required=True)
    ap.add_argument("--provenance", required=True, choices=["measured", "hand"])
    ap.add_argument("--note", default=None)
    ap.add_argument("--set", action="append", default=[], dest="sets")
    args = ap.parse_args()

    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    values = _parse_sets(args.sets, set(ledger["kpis"]))

    snapshot = {
        "ts": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label": args.label,
        "values": {k: values.get(k) for k in ledger["kpis"]},
        "provenance": args.provenance,
        "notes": args.note,
    }
    ledger["snapshots"].append(snapshot)
    LEDGER.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] appended {args.label!r} ({args.provenance}) to {LEDGER.name}")
    print(_trend_table(ledger))

    lost = values.get("ear_vocal_lost")
    if lost is not None and lost > 0:
        sys.exit(
            f"[ERROR] ear_vocal_lost={lost:g} recorded - a rated vocal track is "
            "being auto-stamped no-lyrics. Snapshot written; treat as a blocker."
        )


if __name__ == "__main__":
    main()
