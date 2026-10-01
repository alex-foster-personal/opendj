"""The one error sink: local JSONL always, Sentry when telemetry is on.

Every record carries a stable error id, a host label, and a build sha.
Build and CI failures are tagged ``kind=build``. Packaged default remains
off (see ``decide_telemetry``); this module still writes the local JSONL so
ids are greppable on nucbox at ``~/jobs/logs/opendj-error-sink.jsonl``.

Stdlib only on the write path. sentry-sdk is imported only when a client is
already active, so ``OPENDJ_TELEMETRY=0`` stays "never imported".
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from collections.abc import Mapping, MutableMapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from apps.shared.telemetry.error_id import stable_error_id

log = logging.getLogger(__name__)

SINK_MAX_BYTES: int = 256 * 1024 * 1024
SINK_MAX_ARCHIVES: int = 2
SINK_EVENTS_PER_MINUTE: int = 120

ERROR_SINK_ENV: str = "OPENDJ_ERROR_SINK_LOG"
HOST_LABEL_ENV: str = "OPENDJ_HOST_LABEL"
BUILD_SHA_ENV: str = "OPENDJ_BUILD_SHA"

KINDS: frozenset[str] = frozenset({"engine", "client", "build"})

_NUCBOX_SINK = Path.home() / "jobs" / "logs" / "opendj-error-sink.jsonl"


@dataclass(frozen=True)
class ErrorEvent:
    """One sink record. JSONL and Sentry tags share these fields."""

    error_id: str
    host: str
    build_sha: str
    kind: str
    source_site: str
    message: str
    timestamp: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def current_host() -> str:
    """OPENDJ_HOST_LABEL, else the machine hostname, else ``unknown``."""
    labeled = (os.environ.get(HOST_LABEL_ENV) or "").strip()
    if labeled:
        return labeled
    return (socket.gethostname() or "unknown").strip() or "unknown"


def current_build_sha() -> str:
    """OPENDJ_BUILD_SHA, else GITHUB_SHA, else ``unknown``."""
    for key in (BUILD_SHA_ENV, "GITHUB_SHA"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def sink_path() -> Path:
    """The one greppable JSONL. Override with OPENDJ_ERROR_SINK_LOG."""
    override = (os.environ.get(ERROR_SINK_ENV) or "").strip()
    if override:
        return Path(override)
    if _NUCBOX_SINK.parent.is_dir():
        return _NUCBOX_SINK
    mac_logs = Path.home() / "Library" / "Logs"
    if sys.platform == "darwin" or mac_logs.is_dir():
        return mac_logs / "opendj" / "error-sink.jsonl"
    return Path.home() / ".local" / "share" / "opendj" / "error-sink.jsonl"


def client_source_site(kind: str, url: str) -> str:
    """Stable client site: kind plus the URL path, not the host or query."""
    path = urlparse(url).path or "/"
    return f"client:{kind}:{path}"


def client_error_message(name: str | None, message: str) -> str:
    return f"{name or 'Error'}: {message}"


def make_event(
    *,
    message: str,
    source_site: str,
    kind: str,
    host: str | None = None,
    build_sha: str | None = None,
) -> ErrorEvent:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}, got {kind!r}")
    return ErrorEvent(
        error_id=stable_error_id(source_site=source_site, message=message),
        host=(host or current_host()).strip() or "unknown",
        build_sha=(build_sha or current_build_sha()).strip() or "unknown",
        kind=kind,
        source_site=source_site,
        message=message,
        timestamp=datetime.now(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
    )


def _utc_minute() -> str:
    return datetime.now(UTC).strftime("%Y%m%d%H%M")


def _archive_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _sink_archives(dest: Path) -> list[Path]:
    base = dest.name
    parent = dest.parent
    return sorted(
        candidate
        for candidate in parent.glob(f"{base}.*")
        if candidate != dest
    )


def sink_files(dest: Path | None = None) -> list[Path]:
    """The sink and its archives, oldest first: what a reader scans to see every record."""
    current = dest or sink_path()
    return [*_sink_archives(current), current]


def _prune_sink_archives(dest: Path) -> None:
    archives = _sink_archives(dest)
    while len(archives) > SINK_MAX_ARCHIVES:
        oldest = archives.pop(0)
        try:
            oldest.unlink(missing_ok=True)
        except OSError:
            log.warning("error sink archive prune failed at %s", oldest, exc_info=True)


def _rotate_sink_if_needed(dest: Path, next_bytes: int) -> None:
    size = dest.stat().st_size if dest.exists() else 0
    if size == 0 or size + next_bytes <= SINK_MAX_BYTES:
        return
    archive = dest.with_name(f"{dest.name}.{_archive_timestamp()}")
    try:
        dest.rename(archive)
        _prune_sink_archives(dest)
    except OSError:
        log.warning("error sink rotation failed at %s", dest, exc_info=True)


class _RateState:
    minute: str = ""
    events: int = 0
    suppressed: int = 0


_rate_state = _RateState()


def reset_rate_state_for_tests() -> None:
    """Clear per-minute counters between tests."""
    _rate_state.minute = ""
    _rate_state.events = 0
    _rate_state.suppressed = 0


def _minute_label(minute_key: str) -> str:
    return (
        f"{minute_key[:4]}-{minute_key[4:6]}-{minute_key[6:8]}"
        f"T{minute_key[8:10]}:{minute_key[10:12]}Z"
    )


def _flush_suppression_summary(dest: Path, minute_key: str, count: int) -> None:
    if count <= 0:
        return
    summary = make_event(
        message=(
            f"error sink: {count} events suppressed in minute "
            f"{_minute_label(minute_key)}"
        ),
        source_site="telemetry:sink:rate-limit",
        kind="engine",
    )
    _write_sink_record(summary, dest)
    _rate_state.events += 1


def _write_sink_record(event: ErrorEvent, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(event.as_dict(), ensure_ascii=False, sort_keys=True) + "\n"
    encoded_bytes = len(encoded.encode("utf-8"))
    _rotate_sink_if_needed(dest, encoded_bytes)
    with dest.open("a", encoding="utf-8") as handle:
        handle.write(encoded)


def append_sink(event: ErrorEvent, path: Path | None = None) -> Path:
    """Append one JSON object. Never raises into the caller: logging an error
    must not become one."""
    dest = path or sink_path()
    try:
        minute_key = _utc_minute()
        if minute_key != _rate_state.minute:
            previous = _rate_state.minute
            suppressed = _rate_state.suppressed
            _rate_state.minute = minute_key
            _rate_state.events = 0
            _rate_state.suppressed = 0
            if previous and suppressed > 0:
                _flush_suppression_summary(dest, previous, suppressed)
        if _rate_state.events >= SINK_EVENTS_PER_MINUTE:
            _rate_state.suppressed += 1
            return dest
        _write_sink_record(event, dest)
        _rate_state.events += 1
    except OSError:
        log.warning("error sink write failed at %s", dest, exc_info=True)
    return dest


def capture_error_event(
    *,
    message: str,
    source_site: str,
    kind: str = "engine",
    host: str | None = None,
    build_sha: str | None = None,
) -> ErrorEvent:
    """Mint id/host/sha, append the local JSONL, return the record."""
    event = make_event(
        message=message,
        source_site=source_site,
        kind=kind,
        host=host,
        build_sha=build_sha,
    )
    append_sink(event)
    return event


def sentry_payload(event: ErrorEvent) -> dict[str, Any]:
    """The Sentry-shaped dict tests assert on. No network."""
    return {
        "message": event.message,
        "level": "error",
        "fingerprint": [event.error_id],
        "tags": {
            "error_id": event.error_id,
            "host": event.host,
            "build_sha": event.build_sha,
            "kind": event.kind,
            "source_site": event.source_site,
        },
    }


def maybe_send_sentry(event: ErrorEvent) -> str | None:
    """Forward to Sentry only when the SDK is already active. Never imports it."""
    if "sentry_sdk" not in sys.modules:
        return None
    try:
        import sentry_sdk

        client = sentry_sdk.get_client()
        if client is None or not client.is_active():
            return None
        payload = sentry_payload(event)
        with sentry_sdk.new_scope() as scope:
            for key, value in payload["tags"].items():
                scope.set_tag(key, value)
            scope.fingerprint = payload["fingerprint"]
            return sentry_sdk.capture_message(payload["message"], level="error")
    except Exception:
        log.warning("error sink Sentry forward failed", exc_info=True)
        return None


def post_build_failure(
    *,
    message: str,
    source_site: str = "build:ci",
    host: str | None = None,
    build_sha: str | None = None,
) -> ErrorEvent:
    """CI / headless-dmg failure: local JSONL plus Sentry if telemetry is on."""
    event = capture_error_event(
        message=message,
        source_site=source_site,
        kind="build",
        host=host,
        build_sha=build_sha,
    )
    maybe_send_sentry(event)
    return event


def event_from_headless_dmg_log(
    text: str,
    *,
    host: str | None = None,
    build_sha: str | None = None,
) -> ErrorEvent | None:
    """Parse a headless dmg log. FAILED verdict -> kind=build event; OK -> None."""
    verdict: str | None = None
    for line in text.splitlines():
        if line.startswith("REPORT TO USER:"):
            verdict = line
    if verdict is None or "FAILED" not in verdict:
        return None
    return make_event(
        message=verdict,
        source_site="build:headless-dmg",
        kind="build",
        host=host,
        build_sha=build_sha,
    )


def attach_identity_to_sentry_event(
    event: MutableMapping[str, Any], record: ErrorEvent
) -> None:
    """Copy id/host/sha/kind onto a Sentry event's tags (dict or list form)."""
    payload = sentry_payload(record)["tags"]
    tags = event.get("tags")
    if tags is None or isinstance(tags, dict):
        merged: dict[str, Any] = dict(tags or {})
        for key, value in payload.items():
            merged.setdefault(key, value)
        event["tags"] = merged
    elif isinstance(tags, list):
        present = {
            pair[0]
            for pair in tags
            if isinstance(pair, (list, tuple)) and pair
        }
        for key, value in payload.items():
            if key not in present:
                tags.append([key, value])
    if not event.get("fingerprint"):
        event["fingerprint"] = [record.error_id]


def _message_from_sentry_event(event: Mapping[str, Any]) -> str:
    exception = event.get("exception")
    if isinstance(exception, dict):
        values = exception.get("values") or []
        if values and isinstance(values[0], dict):
            name = values[0].get("type") or "Error"
            value = values[0].get("value") or ""
            return f"{name}: {value}"
    message = event.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, dict) and isinstance(message.get("formatted"), str):
        return str(message["formatted"])
    return "unknown error"


def _kind_from_sentry_event(event: Mapping[str, Any]) -> str:
    tags = event.get("tags") or {}
    if isinstance(tags, dict):
        kind = tags.get("kind")
        if kind in KINDS:
            return str(kind)
        if tags.get("origin") == "browser":
            return "client"
    return "engine"


def _source_site_from_hint(hint: Mapping[str, Any] | None) -> str:
    exc_info = (hint or {}).get("exc_info")
    if not exc_info or len(exc_info) < 3 or exc_info[2] is None:
        return "engine:uncaught"
    traceback = exc_info[2]
    frames = []
    while traceback is not None:
        frames.append(traceback)
        traceback = traceback.tb_next
    for traceback in reversed(frames):
        filename = traceback.tb_frame.f_code.co_filename
        if "site-packages" in filename or "sentry_sdk" in filename:
            continue
        name = Path(filename).name
        func = traceback.tb_frame.f_code.co_name
        return f"engine:{name}:{func}"
    return "engine:uncaught"


def event_error_id(event: Mapping[str, Any]) -> str | None:
    tags = event.get("tags")
    if isinstance(tags, dict):
        value = tags.get("error_id")
        return str(value) if value else None
    if isinstance(tags, list):
        for pair in tags:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2 and pair[0] == "error_id":
                return str(pair[1])
    return None


def enrich_sentry_event(
    event: MutableMapping[str, Any], hint: Mapping[str, Any] | None = None
) -> None:
    """Attach identity to a Sentry event; write the local sink if not yet written.

    Called from before_send. If capture_browser_error already stamped error_id
    on the scope, this does not append a second JSONL row.
    """
    if event_error_id(event):
        return
    record = capture_error_event(
        message=_message_from_sentry_event(event),
        source_site=_source_site_from_hint(hint),
        kind=_kind_from_sentry_event(event),
    )
    attach_identity_to_sentry_event(event, record)
