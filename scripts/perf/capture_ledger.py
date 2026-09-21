"""Append-only KPI ledger rows for browser perf captures."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

CAPTURE_ID = "perf-capture"
ROUND = "issue-1885"
SOURCE = (
    "client-telemetry login span (submit mark to first recordLibraryLoadTiming); "
    "scored value is in-app (Google consent excluded)"
)
METHOD = (
    "client-telemetry markLoginSubmit/markLoginNavigate to recordLibraryLoadTiming "
    "(in-app, Google excluded)"
)
SPAN_NAME = "login-submit-to-library-usable"
RESTORED_SESSION_PREFIX = "restored-session:"
MISSING_TELEMETRY_PREFIX = "missing-telemetry:"


def classify_s13_withhold_reason(reason: str | None) -> str:
    """Classify withheld S13 capture failures for operator-visible stderr."""
    if reason is None:
        return (
            f"{MISSING_TELEMETRY_PREFIX} no perf-span POST "
            "(library-usable telemetry hooks absent or library did not reach first paint)"
        )
    if reason.startswith((RESTORED_SESSION_PREFIX, MISSING_TELEMETRY_PREFIX)):
        return reason
    if reason == "missing library-usable mark: no perf-span POST":
        return (
            f"{MISSING_TELEMETRY_PREFIX} login completed but no perf-span POST "
            "(library-usable telemetry hooks absent or library did not reach first paint)"
        )
    lowered = reason.lower()
    if "submit" in lowered and "mark" in lowered:
        return f"{RESTORED_SESSION_PREFIX} {reason}"
    return reason


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _span_is_scorable(span: dict[str, Any]) -> tuple[bool, str | None]:
    if span.get("kind") != "perf-span":
        return False, "span kind is not perf-span"
    if span.get("name") != SPAN_NAME:
        return False, f"span name is not {SPAN_NAME}"
    stages = span.get("stages")
    if not isinstance(stages, dict):
        return False, "span stages missing"
    pre = stages.get("pre_navigate_ms")
    post = stages.get("post_navigate_ms")
    if not _is_finite_number(pre) or not _is_finite_number(post):
        return False, "cannot exclude Google: missing pre_navigate_ms or post_navigate_ms"
    duration_ms = span.get("duration_ms")
    if not _is_finite_number(duration_ms) or duration_ms < 0:
        return False, "duration_ms is not a finite non-negative number"
    if abs(duration_ms - (pre + post)) > 1.0:
        return False, "duration_ms does not equal pre_navigate_ms + post_navigate_ms"
    full_wall_ms = stages.get("full_wall_ms")
    if not _is_finite_number(full_wall_ms) or full_wall_ms < 0:
        return False, "full_wall_ms is not a finite non-negative number"
    return True, None


@dataclass(frozen=True)
class _LedgerRowCtx:
    sha: str
    machine: str
    capture_date: date
    note: str
    status: str | None = None
    measured: bool | None = None


def _base_row(
    kpi: str,
    value: float | None,
    unit: str,
    ctx: _LedgerRowCtx,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "date": ctx.capture_date.isoformat(),
        "round": ROUND,
        "kpi": kpi,
        "value": value,
        "unit": unit,
        "machine": ctx.machine,
        "source": SOURCE,
        "method": METHOD,
        "sha": ctx.sha,
        "capture_id": CAPTURE_ID,
        "note": ctx.note,
    }
    if ctx.status is not None:
        row["status"] = ctx.status
    if ctx.measured is not None:
        row["measured"] = ctx.measured
    return row


def span_to_ledger_rows(
    span: dict[str, Any] | None,
    *,
    sha: str,
    machine: str,
    capture_date: date,
    reason: str | None = None,
) -> list[dict[str, Any]]:
    """Convert a client-events perf-span into scored and context ledger rows."""
    if span is None:
        withheld_reason = reason or "missing library-usable mark"
        ctx = _LedgerRowCtx(
            sha=sha,
            machine=machine,
            capture_date=capture_date,
            note=f"UNKNOWN: {withheld_reason}",
            status="withheld",
            measured=False,
        )
        return [
            _base_row("login_submit_to_library_usable_s", None, "s", ctx),
            _base_row("login_full_wall_s", None, "s", ctx),
        ]

    ok, span_reason = _span_is_scorable(span)
    if not ok:
        withheld_reason = reason or span_reason or "span failed validation"
        ctx = _LedgerRowCtx(
            sha=sha,
            machine=machine,
            capture_date=capture_date,
            note=f"UNKNOWN: {withheld_reason}",
            status="withheld",
            measured=False,
        )
        return [
            _base_row("login_submit_to_library_usable_s", None, "s", ctx),
            _base_row("login_full_wall_s", None, "s", ctx),
        ]

    stages = span["stages"]
    duration_ms = float(span["duration_ms"])
    full_wall_ms = float(stages["full_wall_ms"])
    provenance_note = (
        "browser capture against running engine; duration_ms is in-app "
        "(pre_navigate + post_navigate); full_wall is context and includes Google"
    )
    scored_ctx = _LedgerRowCtx(
        sha=sha, machine=machine, capture_date=capture_date, note=provenance_note,
    )
    wall_ctx = _LedgerRowCtx(
        sha=sha,
        machine=machine,
        capture_date=capture_date,
        note="context only; includes Google consent wait; does not score S13",
    )
    return [
        _base_row("login_submit_to_library_usable_s", duration_ms / 1000.0, "s", scored_ctx),
        _base_row("login_full_wall_s", full_wall_ms / 1000.0, "s", wall_ctx),
    ]


def _infer_json_indent(text: str) -> int:
    """Return the positive space indent width used by top-level JSON properties."""
    indents: list[int] = []
    for line in text.splitlines():
        match = re.match(r'^( +)"[^"]+"\s*:', line)
        if match:
            indents.append(len(match.group(1)))
    if not indents:
        raise ValueError(
            "cannot infer JSON indent from ledger: no indented property lines found"
        )
    width = min(indents)
    if width < 1:
        raise ValueError(
            "cannot infer JSON indent from ledger: indent width must be positive"
        )
    return width


def append_ledger_rows(ledger_path: Path, rows: list[dict[str, Any]]) -> None:
    """Append rows to the KPI ledger without rewriting historical entries."""
    original = ledger_path.read_text(encoding="utf-8")
    indent = _infer_json_indent(original)
    payload = json.loads(original)
    entries = payload.setdefault("entries", [])
    entries.extend(rows)
    ledger_path.write_text(
        json.dumps(payload, indent=indent, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
