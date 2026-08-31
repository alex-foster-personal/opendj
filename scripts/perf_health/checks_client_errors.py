"""Check 1: client-error JSONLs, scanned for deck-load failures and Signalsmith timeouts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import (
    CLIENT_ERROR_PREFIX,
    DECK_LOAD_CONTEXT_SOURCE,
    DECK_LOAD_POINTER,
    DECK_LOAD_SIGNATURES,
    MAX_EVIDENCE_ROWS,
    SEVERITY_OK,
    SEVERITY_WARN,
    SIGNALSMITH_POINTER,
    SIGNALSMITH_SIGNATURES,
    STAGE_PREFIX,
)
from .models import CheckResult, Finding
from .util import LOG_DATE_RE, _log_files, _plural, _read_lines, _window_label


def _classify(record: dict) -> tuple[str, str] | None:
    """(family, signature) for a row worth reporting, or None.

    Families are kept apart because they point at different evidence: a Signalsmith
    timeout is a worklet/xrun question, a deck-load failure is a stage-timing question.
    """
    message = str(record.get("message", ""))
    for name, pattern in SIGNALSMITH_SIGNATURES:
        if pattern.search(message):
            return "signalsmith", name
    context = record.get("context")
    if isinstance(context, dict) and context.get("source") == DECK_LOAD_CONTEXT_SOURCE:
        return "deck-load", "deck-load-context"
    for name, pattern in DECK_LOAD_SIGNATURES:
        if pattern.search(message):
            return "deck-load", name
    return None


def _render_context(record: dict) -> str:
    """Stage timings heaviest-first, then the rest of the context.

    PR #539 flattens a failed load's stage map into ``stage_<name>`` keys, and the
    heaviest stage is the whole reason the context is attached, so it leads.
    """
    context = record.get("context")
    if not isinstance(context, dict) or not context:
        return ""
    stages = {k: v for k, v in context.items() if k.startswith(STAGE_PREFIX)}
    numeric = {k: v for k, v in stages.items() if isinstance(v, (int, float))}
    ordered = sorted(numeric.items(), key=lambda item: item[1], reverse=True)
    ordered += sorted((k, v) for k, v in stages.items() if k not in numeric)
    ordered += sorted((k, v) for k, v in context.items() if not k.startswith(STAGE_PREFIX))
    return " ".join(f"{key}={value}" for key, value in ordered)


def _finding(record: dict, family: str, signature: str) -> Finding:
    when = str(record.get("received_at") or record.get("client_timestamp") or "unknown-time")
    pointer = SIGNALSMITH_POINTER if family == "signalsmith" else DECK_LOAD_POINTER
    label = "Signalsmith timeout" if family == "signalsmith" else "deck-load failure"
    return Finding(
        when=when,
        signature=signature,
        message=str(record.get("message", "")).replace("\n", " ")[:240],
        context=_render_context(record),
        pointer=f"{label} at {when}: {pointer}",
    )


@dataclass
class _ScanTally:
    """Running counts for one client-error scan, so every printed number has a source."""

    rows: int = 0
    malformed: list[str] = field(default_factory=list)
    per_day: dict = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    def note_day(self, day: str, family: str) -> None:
        bucket = self.per_day.setdefault(day, {"rows": 0, "signalsmith": 0, "deck-load": 0})
        bucket[family] += 1


def _scan_client_errors(paths: list[Path]) -> _ScanTally:
    tally = _ScanTally()
    for path in paths:
        stamp = LOG_DATE_RE.search(path.name)
        day = stamp.group(1) if stamp else "unknown-day"
        bucket = tally.per_day.setdefault(day, {"rows": 0, "signalsmith": 0, "deck-load": 0})
        for number, line in enumerate(_read_lines(path), start=1):
            if not line.strip():
                continue
            tally.rows += 1
            bucket["rows"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                tally.malformed.append(f"{path}:{number} ({exc.msg})")
                continue
            if not isinstance(record, dict):
                kind = type(record).__name__
                tally.malformed.append(f"{path}:{number} (row is {kind}, not an object)")
                continue
            hit = _classify(record)
            if hit is None:
                continue
            family, signature = hit
            tally.note_day(day, family)
            tally.findings.append(_finding(record, family, signature))
    return tally


def _client_error_detail(tally: _ScanTally, paths: list[Path], days: int) -> str:
    signalsmith = sum(day["signalsmith"] for day in tally.per_day.values())
    deck_load = sum(day["deck-load"] for day in tally.per_day.values())
    return (
        f"{deck_load} deck-load failures and {signalsmith} Signalsmith timeouts "
        f"in {_plural(tally.rows, 'row')} across {_plural(len(paths), 'daily log')} "
        f"({_window_label(days)}), {_plural(len(tally.malformed), 'malformed row')}"
    )


def _client_error_breakdown(tally: _ScanTally) -> list[str]:
    """Per-day counts against that day's own row count, because a rate needs its day."""
    return [
        f"{day}: {counts['deck-load']} deck-load, {counts['signalsmith']} signalsmith "
        f"in {_plural(counts['rows'], 'row')}"
        for day, counts in sorted(tally.per_day.items())
    ]


def check_client_errors(
    log_dirs: tuple[Path, ...], days: int, now: datetime
) -> CheckResult:
    window = _window_label(days)
    paths, present_roots = _log_files(log_dirs, CLIENT_ERROR_PREFIX, days, now)
    if not present_roots:
        return CheckResult(
            check="client-errors",
            severity=SEVERITY_WARN,
            classification="sink-absent",
            detail="no client-error log root exists on this machine, so 0 rows were readable",
            window=window,
            remediation=(
                "none of the expected roots exist: "
                + ", ".join(str(directory) for directory in log_dirs)
            ),
        )
    if not paths:
        roots = ", ".join(str(root) for root in present_roots)
        return CheckResult(
            check="client-errors",
            severity=SEVERITY_WARN,
            classification="no-logs-in-window",
            detail=f"0 daily logs inside the {window} across {len(present_roots)} roots",
            window=window,
            sources=[str(root) for root in present_roots],
            remediation=f"the app has not written a client-error log in the {window} ({roots})",
        )

    tally = _scan_client_errors(paths)
    detail = _client_error_detail(tally, paths, days)
    severity = SEVERITY_WARN if (tally.findings or tally.malformed) else SEVERITY_OK
    classification = "healthy"
    if tally.findings:
        classification = "findings"
    elif tally.malformed:
        classification = "malformed-rows"
    return CheckResult(
        check="client-errors",
        severity=severity,
        classification=classification,
        detail=detail,
        window=window,
        sources=[str(path) for path in paths],
        breakdown=_client_error_breakdown(tally),
        findings=tally.findings,
        remediation=(
            "; ".join(tally.malformed[:MAX_EVIDENCE_ROWS]) if tally.malformed else ""
        ),
    )
