"""Shared JSONL append and UTC timestamp helpers for voice audit logs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Read-only intent latency budget (voice-feasibility / CONTEXT criterion 7).
READ_ONLY_LATENCY_BUDGET_MS: float = 1_800.0


def now_iso() -> str:
    """UTC timestamp with millisecond precision and a trailing ``Z``."""
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    """Append a single JSON line to ``path`` (creating parent dir)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, sort_keys=True))
        fh.write("\n")
