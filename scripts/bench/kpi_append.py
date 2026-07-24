# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Append a KPI snapshot to kpi_ledger.json and print the KPIs-by-snapshot table.

Usage:
    uv run scripts/bench/kpi_append.py --label <run-label> --set key=value [--set key=value ...]

Every --set key must exist in the ledger's "kpis" dict (unknown key = hard error listing
valid keys). Unspecified keys are recorded as null. Snapshot ts = now (UTC, ISO 8601 Z).
Fail-fast: no hidden defaults, no silent skips.

Acceptance:
- [if] a --set names a key absent from kpis [then] exit non-zero listing the valid keys.
- [if] a --set value is not a number [then] exit non-zero naming the offending pair.
- [if] all --set keys are valid [then] a new snapshot appends and the table prints to stdout.
- [if] a KPI is not passed [then] its cell for the new snapshot reads null.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

LEDGER_PATH = Path(__file__).with_name("kpi_ledger.json")


#----- io + validation ------------------------------------------------------


def _load_ledger(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"[ERROR] ledger not found: {path}")
    ledger = json.loads(path.read_text())
    if "kpis" not in ledger or "snapshots" not in ledger:
        sys.exit(f"[ERROR] {path} missing 'kpis' or 'snapshots' key")
    return ledger


def _parse_set(pairs: list[str], valid_keys: list[str]) -> dict[str, float]:
    values: dict[str, float] = {}
    for pair in pairs:
        if "=" not in pair:
            sys.exit(f"[ERROR] --set expects key=value, got: {pair!r}")
        key, raw = pair.split("=", 1)
        key = key.strip()
        if key not in valid_keys:
            keys = ", ".join(valid_keys)
            sys.exit(f"[ERROR] unknown KPI key {key!r}. Valid keys: {keys}")
        try:
            values[key] = float(raw)
        except ValueError:
            sys.exit(f"[ERROR] value for {key!r} is not a number: {raw!r}")
    return values


#----- table render ---------------------------------------------------------


def _fmt(value: float | int | None) -> str:
    if value is None:
        return "null"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def _render_table(ledger: dict) -> str:
    kpis = ledger["kpis"]
    snapshots = ledger["snapshots"]
    key_w = max([len("KPI")] + [len(k) for k in kpis])
    col_w = [max(len(s["label"]), 8) for s in snapshots]

    def row(cells: list[str]) -> str:
        head = cells[0].ljust(key_w)
        body = "  ".join(c.rjust(col_w[i]) for i, c in enumerate(cells[1:]))
        return f"{head}  {body}"

    def divider() -> str:
        return "-" * key_w + "  " + "  ".join("-" * w for w in col_w)

    lines = [row(["KPI"] + [s["label"] for s in snapshots]), divider()]
    for key in kpis:
        cells = [key] + [_fmt(s["values"].get(key)) for s in snapshots]
        lines.append(row(cells))
    return "\n".join(lines)


#----- main -----------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Append a KPI snapshot to kpi_ledger.json.")
    parser.add_argument("--label", required=True, help="short run label for the snapshot column")
    parser.add_argument("--set", dest="sets", action="append", default=[],
                        metavar="key=value", help="set one KPI value; repeatable")
    args = parser.parse_args()

    ledger = _load_ledger(LEDGER_PATH)
    valid_keys = list(ledger["kpis"].keys())
    set_values = _parse_set(args.sets, valid_keys)

    snapshot = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label": args.label,
        "values": {key: set_values.get(key) for key in valid_keys},
    }
    ledger["snapshots"].append(snapshot)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2) + "\n")

    print(f"[OK] appended snapshot {args.label!r} ({snapshot['ts']})")
    print(_render_table(ledger))


if __name__ == "__main__":
    main()
