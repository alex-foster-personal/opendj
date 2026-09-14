"""Append-only writer for docs/perf/kpi-ledger.json used by perf KPI nightly."""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

_REQUIRED = ("date", "round", "kpi", "unit", "machine", "source", "note")
_ENTRIES_CLOSE_RE = re.compile(r"\n(?P<ws>\s*)\](?P<tail>\s*\}\s*)$")
_EMPTY_ENTRIES_RE = re.compile(r'"entries"\s*:\s*\[\s*\]')
_COMPACT_CLOSE_RE = re.compile(r"\](?P<tail>\s*\}\s*)$")


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


def _format_new_entries(rows: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in rows:
        dumped = json.dumps(row, indent=1, ensure_ascii=False)
        indented = "\n".join(f"  {line}" if line else line for line in dumped.split("\n"))
        parts.append(indented)
    return ",\n".join(parts)


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except Exception:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


def _splice_entries(stripped: str, rows: list[dict[str, Any]]) -> str:
    formatted = _format_new_entries(rows)
    matches = list(_ENTRIES_CLOSE_RE.finditer(stripped))
    if matches:
        match = matches[-1]
        # Keep the previous last entry's own line byte-for-byte, no trailing
        # comma added: consuming match.start() (the "\n" already following
        # it) into the untouched prefix and adding the separator as a NEW
        # line above the array's closing bracket. Splicing the comma onto
        # the end of that existing line instead would turn "  }" into
        # "  },", which git reports as a modified line (1 deletion) rather
        # than a pure append -- exactly what test_append_preserves_shipped_
        # ledger_bytes checks for, since a nightly ledger append must never
        # touch history.
        preserved_end = match.start() + 1
        return (
            stripped[:preserved_end]
            + match.group("ws")
            + ",\n"
            + formatted
            + "\n"
            + match.group("ws")
            + "]"
            + match.group("tail")
            + "\n"
        )

    compact = _COMPACT_CLOSE_RE.search(stripped)
    if compact is not None:
        bracket_pos = compact.start()
        last_brace = stripped.rfind("}", 0, bracket_pos)
        if last_brace < 0:
            raise ValueError("cannot locate last entry close in compact ledger")
        return (
            stripped[: last_brace + 1]
            + ",\n"
            + formatted
            + stripped[last_brace + 1 : bracket_pos]
            + "]"
            + compact.group("tail")
            + "\n"
        )

    raise ValueError("cannot locate entries array close in ledger")


def append_entries(path: Path, rows: list[dict[str, Any]], *, validate: bool = True) -> None:
    if not rows:
        return
    if validate:
        rows = [validate_entry(row) for row in rows]

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        doc = {"schema_version": 2, "entries": rows}
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        return

    text = path.read_text(encoding="utf-8")
    stripped = text.rstrip("\n")
    empty_match = _EMPTY_ENTRIES_RE.search(stripped)
    if empty_match is not None:
        formatted = _format_new_entries(rows)
        new_array = "[\n" + formatted + "\n]"
        new_text = (
            stripped[: empty_match.start()]
            + '"entries": '
            + new_array
            + stripped[empty_match.end() :]
            + "\n"
        )
        _atomic_write(path, new_text)
        return

    _atomic_write(path, _splice_entries(stripped, rows))
