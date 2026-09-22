"""Scan diagnostic sinks and merge ERROR/WARN records for a time window."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

DEFAULT_WINDOW_SECONDS = 3600
MAX_WINDOW_DAYS = 7
MAX_EVENTS = 1000

ContextValue = str | int | float | bool | None

# Keep-rules mirror ops.agentic_testing.ledger._detect_plain_text_level.
_SHELL_LEVEL_RE = re.compile(r"\[shell\s+(\w+)\]", re.IGNORECASE)
_PREFIX_LEVEL_RE = re.compile(r"^(ERROR|WARNING|WARN):", re.IGNORECASE)
_TOKEN_LEVEL_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*\s+)?(ERROR|WARNING|WARN)\b",
    re.IGNORECASE,
)
_ISO_PREFIX_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)
_SIMPLE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")


def _canonical_level(raw: str) -> Literal["ERROR", "WARN"] | None:
    upper = raw.upper()
    if upper in {"ERROR", "CRITICAL", "FATAL", "PANIC"}:
        return "ERROR"
    if upper in {"WARN", "WARNING"}:
        return "WARN"
    return None


def _detect_plain_text_level(line: str) -> Literal["ERROR", "WARN"] | None:
    stripped = line.strip()
    if not stripped:
        return None
    shell_match = _SHELL_LEVEL_RE.match(stripped)
    if shell_match:
        return _canonical_level(shell_match.group(1))
    prefix_match = _PREFIX_LEVEL_RE.match(stripped)
    if prefix_match:
        return _canonical_level(prefix_match.group(1))
    token_match = _TOKEN_LEVEL_RE.match(stripped)
    if token_match:
        return _canonical_level(token_match.group(1))
    return None


def _parse_timestamp(value: str) -> datetime | None:
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_line_timestamp(line: str) -> datetime | None:
    iso_match = _ISO_PREFIX_RE.match(line.strip())
    if iso_match:
        return _parse_timestamp(iso_match.group(1).replace(" ", "T"))
    simple_match = _SIMPLE_TS_RE.match(line.strip())
    if simple_match:
        return _parse_timestamp(simple_match.group(1).replace(" ", "T"))
    return None


def _format_received_at(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_fault_time(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        millis = float(value)
        if millis > 1_000_000_000_000:
            return datetime.fromtimestamp(millis / 1000.0, tz=UTC)
        return datetime.fromtimestamp(millis, tz=UTC)
    if isinstance(value, str):
        return _parse_timestamp(value)
    return None


@dataclass
class ErrorEvent:
    source: str
    received_at: str
    level: Literal["ERROR", "WARN"]
    kind: str
    message: str
    stack: str | None = None
    url: str | None = None
    context: dict[str, ContextValue] | None = None
    event_id: str | None = None


@dataclass
class SinkStatus:
    id: str
    available: bool
    roots: int | None = None
    files: int | None = None
    unreadable_lines: int | None = None
    reason: str | None = None


@dataclass
class ErrorFeedResult:
    since: str
    until: str
    truncated: bool
    sinks: list[SinkStatus]
    events: list[ErrorEvent]


@dataclass
class _ScanState:
    unreadable_lines: int = 0


def _in_window(value: datetime, since: datetime, until: datetime) -> bool:
    return since <= value <= until


def _client_error_logs(log_dir: Path) -> list[Path]:
    return sorted(log_dir.glob("webui-client-errors-????-??-??.log"))


def _scan_client_errors(
    primary_dir: Path,
    legacy_dir: Path | None,
    since: datetime,
    until: datetime,
) -> tuple[list[ErrorEvent], SinkStatus]:
    state = _ScanState()
    seen_ids: set[str] = set()
    events: list[ErrorEvent] = []
    roots = 1
    dirs = [primary_dir]
    if legacy_dir is not None and legacy_dir != primary_dir:
        dirs.append(legacy_dir)
        roots = 2
    for log_dir in dirs:
        for path in _client_error_logs(log_dir):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    state.unreadable_lines += 1
                    continue
                if not isinstance(record, dict):
                    state.unreadable_lines += 1
                    continue
                event_id = str(record.get("event_id", ""))
                if event_id and event_id in seen_ids:
                    continue
                received_raw = record.get("received_at")
                if not isinstance(received_raw, str):
                    state.unreadable_lines += 1
                    continue
                received_at = _parse_timestamp(received_raw)
                if received_at is None or not _in_window(received_at, since, until):
                    continue
                if event_id:
                    seen_ids.add(event_id)
                kind = str(record.get("kind", "client-error"))
                message = str(record.get("message", ""))
                level: Literal["ERROR", "WARN"] = (
                    "WARN" if kind == "console-warn" else "ERROR"
                )
                context_raw = record.get("context")
                context = (
                    {str(k): v for k, v in context_raw.items()}
                    if isinstance(context_raw, dict)
                    else None
                )
                events.append(
                    ErrorEvent(
                        source="client-errors",
                        received_at=_format_received_at(received_at),
                        level=level,
                        kind=kind,
                        message=message,
                        stack=record.get("stack")
                        if isinstance(record.get("stack"), str)
                        else None,
                        url=record.get("url")
                        if isinstance(record.get("url"), str)
                        else None,
                        context=context,
                        event_id=event_id or None,
                    )
                )
    return events, SinkStatus(
        id="client-errors",
        available=True,
        roots=roots,
        unreadable_lines=state.unreadable_lines,
    )


def _engine_log_files(log_dir: Path, base_name: str) -> list[Path]:
    live = log_dir / base_name
    paths: list[Path] = []
    if live.exists():
        paths.append(live)
    for path in sorted(log_dir.glob(f"{base_name}.*")):
        if path == live:
            continue
        paths.append(path)
    return paths


def _scan_plain_engine_log(
    path: Path,
    since: datetime,
    until: datetime,
    _state: _ScanState,
) -> list[ErrorEvent]:
    events: list[ErrorEvent] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return events
    file_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    last_ts = file_mtime
    for line in text.splitlines():
        parsed_ts = _parse_line_timestamp(line)
        if parsed_ts is not None:
            last_ts = parsed_ts
        level = _detect_plain_text_level(line)
        if level is None:
            continue
        if not _in_window(last_ts, since, until):
            continue
        events.append(
            ErrorEvent(
                source="engine.log",
                received_at=_format_received_at(last_ts),
                level=level,
                kind="engine-log",
                message=line.strip(),
            )
        )
    return events


def _scan_engine_logs(
    log_dir: Path,
    since: datetime,
    until: datetime,
) -> tuple[list[ErrorEvent], SinkStatus]:
    state = _ScanState()
    events: list[ErrorEvent] = []
    files = _engine_log_files(log_dir, "engine.log")
    for path in files:
        events.extend(_scan_plain_engine_log(path, since, until, state))
    return events, SinkStatus(
        id="engine.log",
        available=log_dir.exists(),
        files=len(files),
        unreadable_lines=state.unreadable_lines,
    )


def _scan_engine_warn(
    log_dir: Path,
    since: datetime,
    until: datetime,
) -> tuple[list[ErrorEvent], SinkStatus]:
    state = _ScanState()
    events: list[ErrorEvent] = []
    files = _engine_log_files(log_dir, "engine-warn.log")
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                state.unreadable_lines += 1
                continue
            if not isinstance(record, dict):
                state.unreadable_lines += 1
                continue
            level_raw = str(record.get("level", ""))
            level = _canonical_level(level_raw)
            if level is None:
                continue
            timestamp_raw = record.get("timestamp")
            if not isinstance(timestamp_raw, str):
                state.unreadable_lines += 1
                continue
            received_at = _parse_timestamp(timestamp_raw)
            if received_at is None or not _in_window(received_at, since, until):
                continue
            events.append(
                ErrorEvent(
                    source="engine-warn.log",
                    received_at=_format_received_at(received_at),
                    level=level,
                    kind="engine-warn",
                    message=str(record.get("message", "")),
                    context={
                        "boot_id": record.get("boot_id"),
                        "logger": record.get("logger"),
                    }
                    if record.get("boot_id") is not None
                    or record.get("logger") is not None
                    else None,
                )
            )
    return events, SinkStatus(
        id="engine-warn.log",
        available=log_dir.exists(),
        files=len(files),
        unreadable_lines=state.unreadable_lines,
    )


def _scan_ui_mirror(
    mirror: dict[str, Any] | None,
    since: datetime,
    until: datetime,
) -> tuple[list[ErrorEvent], SinkStatus]:
    if mirror is None:
        return [], SinkStatus(
            id="ui-mirror",
            available=False,
            reason="no page open",
        )
    events: list[ErrorEvent] = []
    audio_health = mirror.get("audio_health")
    faults: list[object] = []
    if isinstance(audio_health, dict):
        recent = audio_health.get("recent_faults")
        if isinstance(recent, list):
            faults = recent
    for fault in faults:
        if not isinstance(fault, dict):
            continue
        fault_time = _parse_fault_time(fault.get("t"))
        if fault_time is None or not _in_window(fault_time, since, until):
            continue
        kind = str(fault.get("kind", "ui-mirror"))
        message = str(fault.get("message", ""))
        events.append(
            ErrorEvent(
                source="ui-mirror",
                received_at=_format_received_at(fault_time),
                level="ERROR",
                kind=kind,
                message=message,
                context={"age_ms": fault.get("age_ms")}
                if fault.get("age_ms") is not None
                else None,
            )
        )
    return events, SinkStatus(id="ui-mirror", available=True)


def _sample_is_error(record: dict[str, object]) -> bool:
    health = record.get("audio_health_level")
    if health in {"warn", "crit"}:
        return True
    decks = record.get("decks")
    if not isinstance(decks, list):
        return False
    for deck in decks:
        if not isinstance(deck, dict):
            continue
        if deck.get("stem_status") == "error":
            return True
        if deck.get("sync_error"):
            return True
        if deck.get("processor_error"):
            return True
    return False


def _sample_error_message(record: dict[str, object]) -> str:
    parts: list[str] = []
    health = record.get("audio_health_level")
    if health in {"warn", "crit"}:
        parts.append(f"audio_health_level={health}")
    decks = record.get("decks")
    if isinstance(decks, list):
        for deck in decks:
            if not isinstance(deck, dict):
                continue
            deck_id = deck.get("deck_id")
            if deck.get("stem_status") == "error":
                parts.append(f"deck {deck_id} stem_status=error")
            if deck.get("sync_error"):
                parts.append(f"deck {deck_id} sync_error: {deck['sync_error']}")
            if deck.get("processor_error"):
                parts.append(
                    f"deck {deck_id} processor_error: {deck['processor_error']}"
                )
    return "; ".join(parts) if parts else "client-performance-sample error"


def _scan_client_samples(
    log_dir: Path,
    since: datetime,
    until: datetime,
) -> tuple[list[ErrorEvent], SinkStatus]:
    state = _ScanState()
    events: list[ErrorEvent] = []
    files = sorted(log_dir.glob("webui-performance-????-??-??.log"))
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                state.unreadable_lines += 1
                continue
            if not isinstance(record, dict):
                state.unreadable_lines += 1
                continue
            if not _sample_is_error(record):
                continue
            received_raw = record.get("received_at")
            if not isinstance(received_raw, str):
                state.unreadable_lines += 1
                continue
            received_at = _parse_timestamp(received_raw)
            if received_at is None or not _in_window(received_at, since, until):
                continue
            decks = record.get("decks")
            deck_context: dict[str, ContextValue] = {}
            if isinstance(decks, list):
                deck_context["decks"] = json.dumps(decks)
            events.append(
                ErrorEvent(
                    source="client-samples",
                    received_at=_format_received_at(received_at),
                    level="ERROR",
                    kind="client-performance-sample",
                    message=_sample_error_message(record),
                    context=deck_context or None,
                    event_id=str(record.get("event_id"))
                    if record.get("event_id") is not None
                    else None,
                )
            )
    return events, SinkStatus(
        id="client-samples",
        available=log_dir.exists(),
        files=len(files),
        unreadable_lines=state.unreadable_lines,
    )


def collect_error_feed(
    *,
    since: datetime,
    until: datetime,
    client_error_log_dir: Path,
    client_error_legacy_log_dir: Path | None = None,
    performance_log_dir: Path | None = None,
    ui_mirror: dict[str, Any] | None = None,
) -> ErrorFeedResult:
    if until < since:
        raise ValueError("until must be >= since")
    if until - since > timedelta(days=MAX_WINDOW_DAYS):
        raise ValueError(f"window must be at most {MAX_WINDOW_DAYS} days")

    perf_dir = performance_log_dir or client_error_log_dir
    all_events: list[ErrorEvent] = []
    sinks: list[SinkStatus] = []

    client_events, client_sink = _scan_client_errors(
        client_error_log_dir,
        client_error_legacy_log_dir,
        since,
        until,
    )
    all_events.extend(client_events)
    sinks.append(client_sink)

    engine_events, engine_sink = _scan_engine_logs(client_error_log_dir, since, until)
    all_events.extend(engine_events)
    sinks.append(engine_sink)

    warn_events, warn_sink = _scan_engine_warn(client_error_log_dir, since, until)
    all_events.extend(warn_events)
    sinks.append(warn_sink)

    mirror_events, mirror_sink = _scan_ui_mirror(ui_mirror, since, until)
    all_events.extend(mirror_events)
    sinks.append(mirror_sink)

    sample_events, sample_sink = _scan_client_samples(perf_dir, since, until)
    all_events.extend(sample_events)
    sinks.append(sample_sink)

    all_events.sort(key=lambda item: item.received_at)
    truncated = len(all_events) > MAX_EVENTS
    if truncated:
        all_events = all_events[:MAX_EVENTS]

    return ErrorFeedResult(
        since=_format_received_at(since),
        until=_format_received_at(until),
        truncated=truncated,
        sinks=sinks,
        events=all_events,
    )


def default_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    end = now or datetime.now(UTC)
    start = end - timedelta(seconds=DEFAULT_WINDOW_SECONDS)
    return start, end
