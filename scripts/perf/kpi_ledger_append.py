"""Append-only writer for docs/perf/kpi-ledger.json used by perf KPI nightly."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REQUIRED = ("date", "round", "kpi", "unit", "machine", "source", "note")


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"kpi ledger missing: {path}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        raise TypeError(f"kpi ledger must be an object with entries[]: {path}")
    return doc


def validate_entry(raw: dict[str, Any]) -> dict[str, Any]:
    for key in _REQUIRED:
        if key not in raw or raw[key] in (None, ""):
            raise ValueError(f"ledger entry missing required field: {key}")
    measured = bool(raw.get("measured", raw.get("value") is not None))
    status = raw.get("status")
    value = raw.get("value")
    if status == "error" or measured is False:
        if value is not None:
            raise ValueError("error ledger entry must not carry a numeric value")
        entry = dict(raw)
        entry["value"] = None
        entry["measured"] = False
        entry.setdefault("status", "error")
        return entry
    if not isinstance(value, (int, float)):
        raise TypeError("measured ledger entry requires a numeric value")
    entry = dict(raw)
    entry["measured"] = True
    return entry


def append_entries(path: Path, rows: list[dict[str, Any]]) -> None:
    doc = load_ledger(path)
    entries = doc["entries"]
    for row in rows:
        entries.append(validate_entry(row))
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    temporary.replace(path)
